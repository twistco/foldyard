"""Opt-in LIVE box+metadata e2e — the gcp box wiring + the metadata emulator + a REAL dev box.

Proves the gcp credential path end to end over the engine socket, and the two properties the
real-time rewire turns on:

  • a running box switches identity when the MINTER's mode flips — with NO box recreate. The box's
    wiring is mode-independent (the real `gcp.GcpPlugin().box_args`), so only the Mac minter is
    reconciled; here we flip the fake minter's GCP_ALLOW_USER_TOKEN live and watch the SAME box's
    token change from its SA token to the user token (within the short refresh window).
  • the emergency user token reaches ONLY the escalatable box (the dev box's `gcp.userEscalatable`
    label). An app-labelled box (gcp.serviceAccount only) gets its SA token even in user mode —
    never yours.

ADAPTED TOPOLOGY (mirrors test_proxy_box_e2e): the metadata emulator (server.py) + a fake minter
run in THIS process; the box reaches the emulator at this container's own IP
(GCE_METADATA_HOST=<self-ip>:<port>). The fake minter stands in for the Mac (it holds no gcloud
creds): it returns a MARKER token per the same decision minter.py makes, so we assert WHICH identity
the box is handed, not a real GCP token. server.py is the real thing under test (label resolution,
the user_escalatable flag it forwards, and its 404-when-the-minter-is-down).

OPT-IN: skipped unless FOLDYARD_E2E=1, an engine is reachable, AND we can discover our own container
network (so a sibling box can reach us). Run it from a container with engine access (the dev box):

    just foldyard test-proxy-e2e        # runs the box e2es (this + the proxy ones)
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from foldyard.plugins import gcp

SERVER_PY = gcp.METADATA_DIR / "server.py"
BOX_IMAGE = "quay.io/podman/stable:latest"

_LOG_SA = "devbox-log-reader@acme-staging.iam.gserviceaccount.com"
_APP_SA = "app-runtime@acme-staging.iam.gserviceaccount.com"


def _engine() -> str:
    for candidate in ("podman", "docker"):
        if _which(candidate):
            return candidate
    return "podman"


def _which(cmd: str) -> bool:
    from shutil import which

    return which(cmd) is not None


def _engine_env() -> dict:
    env = dict(os.environ)
    if not env.get("CONTAINER_HOST") and env.get("DOCKER_HOST"):
        env["CONTAINER_HOST"] = env["DOCKER_HOST"]
    return env


def _engine_reachable() -> bool:
    eng = _engine()
    if not _which(eng):
        return False
    try:
        return (
            subprocess.run(
                [eng, "info"], env=_engine_env(), capture_output=True, timeout=20
            ).returncode
            == 0
        )
    except Exception:
        return False


def _self_on_network() -> tuple[str, str] | None:
    """This container's (network, ipv4) — so a sibling box can reach the emulator we run in-process.
    None when not in an engine-connected container (the signal to skip)."""
    if not _engine_reachable():
        return None
    try:
        out = subprocess.run(
            [_engine(), "inspect", socket.gethostname(), "--format",
             "{{range $n, $c := .NetworkSettings.Networks}}{{$n}} {{$c.IPAddress}}\n{{end}}"],
            env=_engine_env(), capture_output=True, text=True, timeout=20,
        )  # fmt: skip
    except Exception:
        return None
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1]:
            return parts[0], parts[1]
    return None


_SELF = _self_on_network()

pytestmark = pytest.mark.skipif(
    os.environ.get("FOLDYARD_E2E") != "1" or not SERVER_PY.exists() or _SELF is None,
    reason=(
        "opt-in box+metadata e2e: set FOLDYARD_E2E=1, have the emulator, and run from a container "
        "with engine access (the dev box)"
    ),
)

_REFRESH = (
    2  # short expires_in ⇒ the emulator re-mints ~every second, so a live mode flip shows fast
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def _wait(predicate, timeout: float, what: str, diag: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}\n{diag}")


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


class _FakeMinter:
    """Stands in for the Mac minter: same decision as minter.py (escalatable + allow_user ⇒ the
    user token; else impersonate an allowlisted SA), but returns a MARKER string instead of calling
    gcloud. `allow_user` is mutable so a test can flip the 'mode' live, exactly as `fy host` would."""

    USER_TOKEN = "USER-TOKEN"

    def __init__(self, allowlist: set[str]) -> None:
        self.allowlist = allowlist
        self.allow_user = False  # flipped live by the test (the gcp=user reconcile)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def _send(self, code: int, obj: dict) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                sa = body.get("sa", "")
                escalatable = bool(body.get("user_escalatable")) or sa == "user"
                if escalatable and outer.allow_user:
                    return self._send(
                        200, {"access_token": outer.USER_TOKEN, "expires_in": _REFRESH}
                    )
                if sa in outer.allowlist:
                    return self._send(200, {"access_token": f"SA:{sa}", "expires_in": _REFRESH})
                return self._send(403, {"error": f"not allowed: {sa}"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), Handler)
        self.port = self._httpd.server_address[1]

    def start(self) -> None:
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._httpd.shutdown()


class _Box:
    def __init__(self, engine: str, env: dict, name: str) -> None:
        self.engine, self.env, self.name = engine, env, name

    def token(self, emulator: str) -> str | None:
        """The access_token the box gets from the emulator's GCE metadata endpoint, or None on a
        non-200 (e.g. the minter-down 404). Uses curl with the anti-SSRF header the libs send."""
        url = f"http://{emulator}/computeMetadata/v1/instance/service-accounts/default/token"
        got = subprocess.run(
            [self.engine, "exec", self.name, "curl", "-s", "-H", "Metadata-Flavor: Google", url],
            env=self.env, capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        try:
            return json.loads(got.stdout).get("access_token")
        except (ValueError, AttributeError):
            return None


def _run_box(engine: str, eng_env: dict, net: str, name: str, labels: list[str], emulator: str):
    """Create a sleep-forever box with the given gcp labels, pointed at the emulator. Returns _Box."""
    run = [
        engine, "run", "-d", "--name", name, "--network", net, "--security-opt", "label=disable",
        *labels, "-e", f"GCE_METADATA_HOST={emulator}", BOX_IMAGE, "sleep", "infinity",
    ]  # fmt: skip
    created = subprocess.run(run, env=eng_env, capture_output=True, text=True, timeout=120)
    assert created.returncode == 0, f"box create failed:\n{created.stderr}"
    return _Box(engine, eng_env, name)


@pytest.fixture(autouse=True)
def _gcp_project_env(monkeypatch):
    """Pin GCP_PROJECT in the TEST PROCESS. The box-label assertions exercise the REAL gcp
    plugin, whose SA emails derive from gcp_project(). config.gcp_project() reads GCP_PROJECT
    first, then [plugins.gcp-metadata].project from the resolved foldyard.toml — and this suite
    runs from foldyard/, whose standalone config has no gcp-metadata table, so without this the
    plugin returns no labels (box_args → []) and the dev-box tests can't get an SA token. The
    emulator subprocess already sees the same value (see metadata_box)."""
    monkeypatch.setenv("GCP_PROJECT", "acme-staging")


@pytest.fixture
def metadata_box(tmp_path):
    """Stand up the REAL emulator (server.py) + a fake minter in-process, and yield a factory that
    creates real boxes on our network pointed at the emulator. Tears everything down after."""
    assert _SELF is not None  # guarded by pytestmark
    net, ip = _SELF
    engine, eng_env = _engine(), _engine_env()
    minter = _FakeMinter(allowlist={_LOG_SA, _APP_SA})
    minter.start()
    sock = (os.environ.get("DOCKER_HOST") or os.environ.get("CONTAINER_HOST") or "").replace(
        "unix://", ""
    ) or "/var/run/docker.sock"
    eport = _free_port()
    log = tmp_path / "emulator.log"
    emu = subprocess.Popen(
        [sys.executable, str(SERVER_PY)],
        env={
            **os.environ,
            "LISTEN_PORT": str(eport),
            "GCP_MINTER_URL": f"http://127.0.0.1:{minter.port}",
            "DOCKER_SOCK": sock,
            "GCP_PROJECT": "acme-staging",
        },
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
    )
    emulator = f"{ip}:{eport}"
    boxes: list[str] = []

    def make(name: str, labels: list[str]) -> _Box:
        full = f"fy-md-e2e-{os.getpid()}-{name}"
        boxes.append(full)
        return _run_box(engine, eng_env, net, full, labels, emulator)

    try:
        _wait(lambda: _port_open(ip, eport), 30, "the metadata emulator to listen",
              log.read_text() if log.exists() else "")  # fmt: skip
        yield minter, emulator, make
    finally:
        for b in boxes:
            subprocess.run([engine, "rm", "-f", b], env=eng_env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)  # fmt: skip
        emu.terminate()
        try:
            emu.wait(timeout=10)
        except subprocess.TimeoutExpired:
            emu.kill()
        minter.stop()


def test_dev_box_switches_identity_live_without_a_recreate(metadata_box):
    minter, emulator, make = metadata_box
    # The dev box, labelled by the REAL plugin (so the userEscalatable marker is exactly what
    # `foldyard box`/`fy box up` bakes) — minus the hard-coded emulator host, which the fixture
    # overrides to our reachable self-ip. The escalatable label is the property under test.
    labels = [a for a in gcp.GcpPlugin().box_args({}) if a != "-e" and not a.startswith("GCE_")]
    assert "gcp.userEscalatable=1" in labels  # the dev box is the escalation target
    box = make("devbox", labels)

    # gcp=logs/sa (minter NOT in user mode): the box impersonates its read-only SA.
    _wait(lambda: box.token(emulator) == f"SA:{_LOG_SA}", 30, "the box's SA token")

    # Flip the minter to gcp=user LIVE — no box recreate, exactly what `fy host` reconcile does.
    minter.allow_user = True
    # Within the short refresh window the SAME box now gets YOUR user token.
    _wait(lambda: box.token(emulator) == _FakeMinter.USER_TOKEN, 30,
          "the box to pick up the user token after the live flip")  # fmt: skip

    # And flipping back (gcp=user → off the escalation) reverts it — again with no recreate.
    minter.allow_user = False
    _wait(lambda: box.token(emulator) == f"SA:{_LOG_SA}", 30, "the box to revert after the flip")


def test_app_box_never_gets_the_user_token(metadata_box):
    minter, emulator, make = metadata_box
    minter.allow_user = True  # gcp=user is ON
    # An app container carries ONLY its SA label — never the escalatable marker.
    app = make("app", ["--label", f"gcp.serviceAccount={_APP_SA}"])
    # Even with user mode on, it gets its OWN SA token, never the user token.
    _wait(lambda: app.token(emulator) == f"SA:{_APP_SA}", 30, "the app's SA token")
    time.sleep(_REFRESH + 1)  # past a refresh, so any escalation would have shown by now
    assert app.token(emulator) == f"SA:{_APP_SA}"
    assert app.token(emulator) != _FakeMinter.USER_TOKEN


def test_emulator_returns_no_token_when_the_minter_is_down(metadata_box):
    minter, emulator, make = metadata_box
    box = make(
        "offbox",
        [a for a in gcp.GcpPlugin().box_args({}) if a != "-e" and not a.startswith("GCE_")],
    )
    _wait(lambda: box.token(emulator) == f"SA:{_LOG_SA}", 30, "the box's SA token")
    # Minter down ≈ gcp=off (the supervisor stops it): the emulator must answer 404, so the box
    # cleanly has NO credentials (rather than a 5xx), and the cached token lapses within the window.
    minter.stop()
    _wait(
        lambda: box.token(emulator) is None, 30, "the box to lose its token once the minter is down"
    )
