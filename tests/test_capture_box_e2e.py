"""Opt-in LIVE capture e2e — the `capture` axis end to end through a REAL dev box.

Where ``test_proxy_box_e2e.py`` drives the INJECTION path (a github rule rewriting a header), this
drives the CAPTURE path: the proxy run with NO injector (logging-only) plus a real box brought up
with the REAL ``capture=on`` wiring, proving the whole "MITM-log all egress" flow Daniel asked
about:

    a real box ──naive HTTPS via $https_proxy, trusting the SYSTEM store──▶ mitmdump (capture-only)
       (`registry().box_args` for capture: CA mount         ──▶ a TLS upstream that echoes the
        + proxy env + NO_PROXY, and the box's system            Authorization header it received
        trust store seeded from the mounted CA by box.py)       (no minter — nothing is rewritten)

It asserts, from the host side + inside the box, that (a) a NAIVE ``curl https://…`` — no
``--cacert``, i.e. relying on the box's SYSTEM trust store — succeeds, proving box-up installed the
MITM CA system-wide (the fix for "curl/wget showed nothing"); (b) capture does NOT rewrite — the
upstream echoes the box's ORIGINAL header, and the egress log records the request with
``injected=False``; (c) the host-grouped Network Log panel (the real ``proxy._network_panel_tree``,
reading via ``config.tail_jsonl``) shows the request under its host with zero injected; and (d)
capture sets the proxy env WITHOUT leaking the github dummy ``GH_TOKEN`` into the box (the
GH_INJECT-marker fix). The daemon spec + box args come from the REAL ``ProxyPlugin``; only the
upstream is a fake.

ADAPTED TOPOLOGY: identical to ``test_proxy_box_e2e.py`` — the proxy + upstream run in THIS process
and the box reaches them at this container's own network IP (``FY_PROXY`` → ``<self-ip>:<port>``),
because the faithful ``foldyard host`` + ``host.containers.internal`` path needs foldyard to run as
the Mac/nested-KVM host, which the in-box guards forbid. The CA mount, proxy env, NO_PROXY, the
system-trust install, and a real box over the real socket are all the genuine article.

OPT-IN: skipped unless FOLDYARD_E2E=1, mitmdump is installed (the `e2e` group), the addon is in the
checkout, an engine is reachable, AND we can discover our own container's network. Run it from a
container with engine access (the dev box / nested host):

    just foldyard test-proxy-e2e        # runs -k "proxy_e2e or proxy_box_e2e or capture_box_e2e"
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import pytest

from e2e_box import BOX_IMAGE
from foldyard import allowlist, config
from foldyard import box as boxmod
from foldyard.plugins import Registry
from foldyard.plugins import proxy as proxy_mod
from foldyard.plugins.github import GithubPlugin
from foldyard.plugins.proxy import BOX_CA, ProxyPlugin

ADDON = Path(__file__).resolve().parents[1] / "src/foldyard/assets/proxy/egress_proxy.py"
# VM-visible scratch for the CA the box mounts (bind sources resolve on the podman MACHINE, so the
# CA must live under the repo, which IS bind-mounted into the VM). See test_proxy_box_e2e.py.
_VM_TMP = Path(__file__).resolve().parents[1] / ".e2e-tmp"


def _engine() -> str:
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    return "podman"


def _engine_env() -> dict:
    env = dict(os.environ)
    if not env.get("CONTAINER_HOST") and env.get("DOCKER_HOST"):
        env["CONTAINER_HOST"] = env["DOCKER_HOST"]
    return env


def _engine_reachable() -> bool:
    eng = _engine()
    if not shutil.which(eng):
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
    """This container's (network, ipv4) so a sibling box can reach the in-process proxy + upstream.
    None when not in a container with a usable network — the signal to skip."""
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
    os.environ.get("FOLDYARD_E2E") != "1"
    or shutil.which("mitmdump") is None
    or not ADDON.exists()
    or _SELF is None,
    reason=(
        "opt-in capture e2e: set FOLDYARD_E2E=1, install the `e2e` group (mitmdump), have the "
        "addon, and run from a container with engine access (the dev box / nested host)"
    ),
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def _self_signed(cert: Path, key: Path, ip: str) -> None:
    """A throwaway upstream cert whose SAN is OUR ip — the proxy re-signs for that host with its own
    CA, and the box verifies against the (system-installed) CA, so the upstream cert's trust is
    moot (mitmdump connects to it with ssl_insecure=true)."""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "upstream")])
    now = datetime.datetime.now(datetime.UTC)
    crt = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(pk.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]), critical=False
        )
        .sign(pk, hashes.SHA256())
    )
    cert.write_bytes(crt.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(
        pk.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


def _make_upstream(port: int, cert: Path, key: Path) -> http.server.ThreadingHTTPServer:
    """A TLS echo upstream on all interfaces: GET /echo → 200 echoing the Authorization header."""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # explicit Content-Length so curl frames the body cleanly

        def do_GET(self):
            body = self.headers.get("Authorization", "<none>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _wait(predicate, timeout: float, what: str, diag: str = "") -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}\n{diag}")


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _log_entries(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


class _Box:
    def __init__(self, engine: str, env: dict, name: str) -> None:
        self.engine, self.env, self.name = engine, env, name

    def exec(self, *cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.engine, "exec", self.name, *cmd],
            env=self.env, capture_output=True, text=True, timeout=60,
        )  # fmt: skip


def _wall_enforcing_with(host: str) -> None:
    """State this test's egress-wall posture: ENFORCING, with ``host`` granted.

    The daemon spec carries the wall (``DEFAULT_DENY`` + ``ALLOW_FILE``), so left unstated the fake
    upstream is judged by whatever is ambient — the repo's own ``[proxy] default_deny`` seed and
    whatever the machine's allow-store happens to hold. That passed on a developer box with grants
    and 403'd every CONNECT in CI, where the seed is `true` and nothing has ever been granted.

    Stating it also earns the test something: the capture path is now proven THROUGH an enforcing
    wall rather than around one. Writes land in the per-test store ``conftest.isolated_allow_store``
    pins, never the real one."""
    allowlist.grant(host, "permanent")
    allowlist.set_wall(True)  # rewrites the effective allowlist the addon re-reads per request


def _seed_system_trust(box: _Box) -> None:
    """Install the mounted MITM CA into the box's SYSTEM trust store with box.py's own snippet —
    the thing under test in every naive-``curl`` assertion below.

    Asserted, not skipped-around. While the rig ran a second base OS this was guarded by a
    "does the image have a trust tool?" skip in each test, which is a silent off switch for the
    load-bearing assertion of this file. The image is ours and pinned (``e2e_box.BOX_IMAGE``), so
    a missing tool is a broken rig and should say so."""
    assert box.exec("sh", "-c", "command -v update-ca-certificates").returncode == 0, (
        f"box image {BOX_IMAGE} has no update-ca-certificates — box.py's CA-trust snippet, and so "
        "every naive-curl assertion here, cannot work on it"
    )
    box.exec("bash", "-lc", boxmod._CA_TRUST_SNIPPET)


@pytest.fixture
def capture_box(tmp_path, monkeypatch):
    """Stand up: a TLS upstream + a real mitmdump driven by the REAL capture-mode daemon spec (NO
    injector → logging-only) in THIS process, and a real box brought up with the REAL
    ``registry().box_args`` capture wiring, with the MITM CA seeded into the box's system trust
    store via box.py's own snippet. Yields (box, upstream-port, self-ip, egress-log)."""
    assert _SELF is not None  # guarded by pytestmark
    net, ip = _SELF
    engine, eng_env = _engine(), _engine_env()
    uport, pport = _free_port(), _free_port()

    # Point the log dir at our tmp BEFORE building the spec, so the daemon's PROXY_LOG_FILE and the
    # panel (proxy._network_panel_tree → config.log_dir()/egress.jsonl) read the same file.
    monkeypatch.setattr(config, "log_dir", lambda: tmp_path)
    log = tmp_path / "egress.jsonl"

    cert, key = tmp_path / "up.crt", tmp_path / "up.key"
    _self_signed(cert, key, ip)
    httpd = _make_upstream(uport, cert, key)

    confdir = tmp_path / "mitm"
    mitm_log = tmp_path / "mitmdump.log"

    # ── the capture daemon, from the REAL ProxyPlugin with capture=on and NO injector ─────────
    # Opt the consumer into the proxy ([proxy] declared) so the always-on daemon exists with no
    # injector — capture rides an opted-in proxy, exactly as a real stack-carrying consumer.
    # [plugins.github] declared too: the ambient dummy-token wiring asserted below is a
    # github-consumer property now (undeclared consumers get no GH_TOKEN at all).
    monkeypatch.setattr(config, "proxy_enabled", lambda: True)
    monkeypatch.setattr(config, "github_declared", lambda: True)
    _wall_enforcing_with(ip)  # before the spec is built — it reads DEFAULT_DENY + ALLOW_FILE
    reg = Registry([GithubPlugin(), ProxyPlugin()])
    spec = reg.desired_daemons({"github": "off", "capture": "on"})["egress-proxy"]
    assert spec["env"]["DEFAULT_DENY"] == "1"  # the wall is up; the upstream is granted through it
    # The wiring under test: capture-only ⇒ EMPTY inject config (egress_proxy logs, rewrites nothing),
    # and the log path is the project log dir we redirected above.
    assert spec["env"]["INJECT_HOST"] == "" and spec["env"]["INJECT_COMMAND"] == ""
    assert spec["env"]["PROXY_LOG_FILE"] == str(log)
    assert "capture" in spec["label"]

    proc = subprocess.Popen(
        ["mitmdump", "-s", str(ADDON), "--listen-host", "0.0.0.0", "--listen-port", str(pport),
         "--set", f"confdir={confdir}", "--set", "ssl_insecure=true",
         "--set", "termlog_verbosity=info"],
        env={**os.environ, **spec["env"]},  # the real capture-mode (empty-inject) wiring
        stdout=mitm_log.open("w"), stderr=subprocess.STDOUT,
    )  # fmt: skip
    ca = confdir / "mitmproxy-ca-cert.pem"

    box = None
    vm_dir = _VM_TMP / str(os.getpid())

    def diag() -> str:
        return mitm_log.read_text() if mitm_log.exists() else "(no mitmdump output)"

    try:
        _wait(lambda: _port_open(ip, pport), 30, "mitmdump to listen", diag())
        _wait(ca.exists, 30, "the mitm CA to be written", diag())

        # The box mounts the CA via box_args' `-v` — whose SOURCE must be VM-visible (bind sources
        # resolve on the machine), so copy the CA under the repo. tmp_path is container-local.
        vm_dir.mkdir(parents=True, exist_ok=True)
        vm_ca = vm_dir / "proxy-ca.pem"
        shutil.copyfile(ca, vm_ca)

        # ── the box run args, from the REAL registry().box_args (the surface under test) ───────
        # FY_PROXY → our reachable self-ip:port (adapted topology). github=off, but the DUMMY
        # GH_TOKEN=x is ambient with the proxy substrate (pre-positioned so the axis flips live);
        # it's inert — the host proxy injects nothing under off. MITMPROXY_CA points box_args at
        # the VM-visible CA.
        monkeypatch.setenv("MITMPROXY_CA", str(vm_ca))
        box_env = {"FY_PROXY": f"{ip}:{pport}", "PODMAN_PROJECT": "fycape2e"}
        plugin_args = reg.box_args(box_env)
        assert f"HTTPS_PROXY=http://{ip}:{pport}" in plugin_args  # egress routed through the proxy
        assert f"{vm_ca}:{BOX_CA}:ro" in plugin_args  # CA mounted for the system-trust install
        assert "GH_TOKEN=x" in plugin_args  # the ambient dummy — inert, never a real token

        name = f"fy-capture-e2e-{os.getpid()}"
        run = [
            engine, "run", "-d", "--name", name, "--network", net,
            "--security-opt", "label=disable", *plugin_args,
            BOX_IMAGE, "sleep", "infinity",
        ]  # fmt: skip
        created = subprocess.run(run, env=eng_env, capture_output=True, text=True, timeout=120)
        assert created.returncode == 0, f"box create failed:\n{created.stderr}"
        box = _Box(engine, eng_env, name)
        _seed_system_trust(box)  # the mounted CA into the SYSTEM store, via box.py's own snippet
        yield box, uport, ip, log
    finally:
        if box is not None:
            subprocess.run(
                [engine, "rm", "-f", box.name], env=eng_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
            )  # fmt: skip
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        httpd.shutdown()
        shutil.rmtree(vm_dir, ignore_errors=True)


def test_capture_logs_naive_box_egress_without_injecting(capture_box):
    box, uport, ip, log = capture_box
    url = f"https://{ip}:{uport}/echo"

    # A NAIVE curl — no --cacert, relying on the box's SYSTEM trust store — must succeed, proving
    # box-up installed the MITM CA system-wide (the "curl showed nothing" fix). curl picks the
    # proxy up from $https_proxy (box_args set it). The freshly-generated mitm CA is in no base
    # image's store, so success can only come from our install → this is also the load-bearing test.
    got = box.exec("curl", "-sS", "-H", "Authorization: Bearer DUMMY", url)
    assert got.returncode == 0, f"naive curl through the capture proxy failed:\n{got.stderr}"
    # Capture LOGS but does not rewrite — the upstream echoes the box's ORIGINAL header.
    assert got.stdout == "Bearer DUMMY", f"capture must not inject; upstream saw {got.stdout!r}"

    # Host-side: the egress log recorded the request for the host, flagged NOT injected.
    _wait(
        lambda: any(e.get("host") == ip and not e.get("injected") for e in _log_entries(log)),
        5,
        "a non-injected capture entry in the egress log",
        log.read_text() if log.exists() else "",
    )

    # The real host-grouped Network Log panel groups it under its host with zero injected.
    tree = proxy_mod._network_panel_tree()
    assert ip in [g.key for g in tree.groups], f"host {ip} missing from the panel: {tree.summary}"
    assert "0 with injected Authorization" in tree.summary


def test_capture_routes_egress_without_leaking_a_real_github_token(capture_box):
    box, _, ip, _ = capture_box
    # capture=on routes the box through the proxy …
    proxied = box.exec("printenv", "HTTPS_PROXY")
    assert proxied.returncode == 0 and f"{ip}:" in proxied.stdout, "HTTPS_PROXY not set in the box"
    # … and with github=off the box holds at most the AMBIENT dummy 'x' (pre-positioned with the
    # proxy substrate so the github axis flips live) — never a real credential. The dummy is inert:
    # no host-side inject rule exists under off, so api.github.com sees the literal 'x' and 401s.
    tok = box.exec("printenv", "GH_TOKEN")
    assert tok.returncode == 0 and tok.stdout.strip() == "x", (
        f"expected the inert dummy GH_TOKEN=x, got: {tok.stdout!r}"
    )


# ── Phase A′ — always-route + runtime-toggleable capture ───────────────────────────────────────


def _launch_mitm(spec: dict, confdir: Path, pport: int, mitm_log: Path):
    """Launch mitmdump with a daemon SPEC's env (so CAPTURE_MODE rides along). Reused to RESTART
    the daemon in the other mode mid-test — the box keeps routing to the same port, proving a
    capture flip needs no box recreate."""
    return subprocess.Popen(
        ["mitmdump", "-s", str(ADDON), "--listen-host", "0.0.0.0", "--listen-port", str(pport),
         "--set", f"confdir={confdir}", "--set", "ssl_insecure=true",
         "--set", "termlog_verbosity=info", "--set", "flow_detail=0"],
        env={**os.environ, **spec["env"]},
        stdout=mitm_log.open("a"), stderr=subprocess.STDOUT,
    )  # fmt: skip


@pytest.fixture
def av_box(tmp_path, monkeypatch):
    """Phase A′: a box brought up with the ALWAYS-ROUTE wiring, and a daemon started in PASSTHROUGH
    (capture=off) that a test can RESTART into full MITM (capture=on) without touching the box.
    Yields a controller: box, ip, upstream port, egress log, the upstream's real cert path IN the
    box, and ``restart(capture_on)``. Specs + box args come from the REAL registry."""
    assert _SELF is not None
    net, ip = _SELF
    engine, eng_env = _engine(), _engine_env()
    uport, pport = _free_port(), _free_port()

    monkeypatch.setattr(config, "log_dir", lambda: tmp_path)
    log = tmp_path / "egress.jsonl"
    cert, key = tmp_path / "up.crt", tmp_path / "up.key"
    _self_signed(cert, key, ip)
    httpd = _make_upstream(uport, cert, key)
    confdir, mitm_log = tmp_path / "mitm", tmp_path / "mitmdump.log"

    # Both daemon specs from the REAL ProxyPlugin: capture=off → passthrough, capture=on → full.
    # Opt the consumer into the proxy ([proxy] declared) so the always-on daemon exists with no
    # injector — Phase A′ always-route is an opted-in-consumer property.
    monkeypatch.setattr(config, "proxy_enabled", lambda: True)
    _wall_enforcing_with(ip)  # before the specs are built — they read DEFAULT_DENY + ALLOW_FILE
    reg = Registry([GithubPlugin(), ProxyPlugin()])
    spec_pass = reg.desired_daemons({"github": "off", "capture": "off"})["egress-proxy"]
    spec_full = reg.desired_daemons({"github": "off", "capture": "on"})["egress-proxy"]
    assert spec_pass["env"]["CAPTURE_MODE"] == "passthrough"
    assert spec_full["env"]["CAPTURE_MODE"] == "full"
    assert spec_pass["env"]["PROXY_LOG_FILE"] == str(log)
    # Both modes carry the wall: passthrough tunnels blind, but it still refuses an ungranted host.
    assert spec_pass["env"]["DEFAULT_DENY"] == spec_full["env"]["DEFAULT_DENY"] == "1"

    proc = _launch_mitm(spec_pass, confdir, pport, mitm_log)
    ca = confdir / "mitmproxy-ca-cert.pem"
    box = None
    vm_dir = _VM_TMP / f"av-{os.getpid()}"

    def diag() -> str:
        return mitm_log.read_text() if mitm_log.exists() else "(no mitmdump output)"

    try:
        _wait(lambda: _port_open(ip, pport), 30, "mitmdump to listen", diag())
        _wait(ca.exists, 30, "the mitm CA to be written", diag())

        # Stage the mitm CA (box_args mounts it) AND the upstream's REAL cert (so a test can curl
        # --cacert it to PROVE passthrough left the real cert intact) under the repo — bind sources
        # resolve on the machine. tmp_path is container-local, so copy to the VM-visible dir.
        vm_dir.mkdir(parents=True, exist_ok=True)
        vm_ca, vm_up = vm_dir / "proxy-ca.pem", vm_dir / "upstream-real.pem"
        shutil.copyfile(ca, vm_ca)
        shutil.copyfile(cert, vm_up)
        monkeypatch.setenv("MITMPROXY_CA", str(vm_ca))

        # The REAL box args — Phase A′ always-routes (FY_PROXY → our reachable self-ip:port).
        box_env = {"FY_PROXY": f"{ip}:{pport}", "PODMAN_PROJECT": "fyav2e"}
        plugin_args = reg.box_args(box_env)
        assert f"HTTPS_PROXY=http://{ip}:{pport}" in plugin_args
        assert "REQUESTS_CA_BUNDLE=/etc/dev-proxy-ca-combined.pem" in plugin_args  # combined bundle

        name = f"fy-av-e2e-{os.getpid()}"
        up_in_box = "/etc/upstream-real.pem"
        run = [
            engine, "run", "-d", "--name", name, "--network", net,
            "--security-opt", "label=disable", *plugin_args,
            "-v", f"{vm_up}:{up_in_box}:ro",
            BOX_IMAGE, "sleep", "infinity",
        ]  # fmt: skip
        created = subprocess.run(run, env=eng_env, capture_output=True, text=True, timeout=120)
        assert created.returncode == 0, f"box create failed:\n{created.stderr}"
        box = _Box(engine, eng_env, name)
        _seed_system_trust(box)  # system trust + the combined bundle, via box.py's own snippet

        controller = {"proc": proc}

        def restart(capture_on: bool) -> None:
            """Reconcile the daemon to the other capture mode (what the supervisor does on a `just
            mode capture=…` flip) — terminate + relaunch on the SAME port; the box is untouched."""
            controller["proc"].terminate()
            try:
                controller["proc"].wait(timeout=10)
            except subprocess.TimeoutExpired:
                controller["proc"].kill()
            controller["proc"] = _launch_mitm(
                spec_full if capture_on else spec_pass, confdir, pport, mitm_log
            )
            _wait(lambda: _port_open(ip, pport), 30, "mitmdump to relisten", diag())

        yield {
            "box": box,
            "ip": ip,
            "uport": uport,
            "log": log,
            "up_ca": up_in_box,
            "restart": restart,
            "spec_full": spec_full,  # tests can set env["PASSTHROUGH_HOSTS"] before restart(True)
        }
    finally:
        if box is not None:
            subprocess.run(
                [engine, "rm", "-f", box.name], env=eng_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
            )  # fmt: skip
        controller_proc = locals().get("controller", {}).get("proc", proc)
        controller_proc.terminate()
        try:
            controller_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            controller_proc.kill()
        httpd.shutdown()
        shutil.rmtree(vm_dir, ignore_errors=True)


def test_passthrough_routes_egress_without_decrypting(av_box):
    box, ip, uport, up_ca = av_box["box"], av_box["ip"], av_box["uport"], av_box["up_ca"]
    url = f"https://{ip}:{uport}/echo"

    # Passthrough (capture=off) blind-tunnels HTTPS: the box does end-to-end TLS against the REAL
    # upstream cert. Trusting THAT cert → curl succeeds AND the upstream echoes the ORIGINAL header
    # (nothing rewritten). This is the always-route egress that capture=off must NOT break.
    got = box.exec("curl", "-sS", "--cacert", up_ca, "-H", "Authorization: Bearer DUMMY", url)
    assert got.returncode == 0, f"passthrough curl (real cert) failed:\n{got.stderr}"
    assert got.stdout == "Bearer DUMMY"

    # The load-bearing proof of NO decryption: trusting ONLY the mitm CA must FAIL — the cert the
    # box saw was the upstream's real one, not a mitm-re-signed one (which `full` would present).
    mitm_only = box.exec("curl", "-sS", "--cacert", "/etc/dev-proxy-ca.pem", url)
    assert mitm_only.returncode != 0, "mitm CA verified a passthrough cert — it WAS decrypted"


def test_capture_toggles_on_a_running_box_without_recreate(av_box):
    box, ip, uport, log = av_box["box"], av_box["ip"], av_box["uport"], av_box["log"]
    url = f"https://{ip}:{uport}/echo"

    # Flip capture on host-side — the daemon reconciles to full MITM; the box is NOT recreated.
    av_box["restart"](capture_on=True)

    # Now a NAIVE curl (system store, which carries the mitm CA) succeeds because full-MITM
    # re-signs with the mitm CA — proving the SAME box now routes through a decrypting proxy …
    got = box.exec("curl", "-sS", "-H", "Authorization: Bearer DUMMY", url)
    assert got.returncode == 0, f"curl after toggling capture on failed:\n{got.stderr}"
    # … and the egress log now has a DECRYPTED request row (method/path/status), not a passthrough
    # tunnel marker — the runtime toggle reached the wire with no box recreate.
    _wait(
        lambda: any(
            e.get("host") == ip and e.get("method") == "GET" and not e.get("passthrough")
            for e in _log_entries(log)
        ),
        5,
        "a decrypted GET row after the capture toggle",
        log.read_text() if log.exists() else "",
    )


def test_capture_on_passes_through_a_trusted_host(av_box):
    box, ip, uport, up_ca = av_box["box"], av_box["ip"], av_box["uport"], av_box["up_ca"]
    url = f"https://{ip}:{uport}/echo"

    # capture=on, but the upstream is TRUSTED (in PASSTHROUGH_HOSTS) → it must be tunnelled, NOT
    # decrypted: the fast/quiet path for the toolchain even while we scrutinise the unknowns.
    av_box["spec_full"]["env"]["PASSTHROUGH_HOSTS"] = ip
    av_box["restart"](capture_on=True)

    # Trusting the upstream's REAL cert succeeds (end-to-end TLS, header untouched) …
    got = box.exec("curl", "-sS", "--cacert", up_ca, "-H", "Authorization: Bearer DUMMY", url)
    assert got.returncode == 0, f"trusted-passthrough curl failed:\n{got.stderr}"
    assert got.stdout == "Bearer DUMMY"
    # … and trusting ONLY the mitm CA FAILS — proof the trusted host was NOT decrypted even under
    # capture=on (a decrypting proxy would have presented a mitm-re-signed cert the mitm CA trusts).
    mitm_only = box.exec("curl", "-sS", "--cacert", "/etc/dev-proxy-ca.pem", url)
    assert mitm_only.returncode != 0, "a trusted host was decrypted under capture=on"
