"""Opt-in LIVE box+proxy e2e — the proxy PLUGIN driving a REAL dev box's egress over the socket.

Where ``test_proxy_e2e.py`` drives a hand-rolled ``requests`` client through the proxy, this drives
an actual **dev-box container** brought up with the REAL ``ProxyPlugin`` wiring, and proves the
box-side of the egress proxy end to end:

    a real box container ──HTTPS via $https_proxy, trusting the mounted CA──▶ mitmdump + egress_proxy
       (run with `registry().box_args(env)`:                ──▶ a TLS "GitHub" upstream that echoes
        the CA mount + proxy env + NO_PROXY,                    the Authorization header it received
        exactly what `foldyard box up` bakes in)               (a fake minter mints "token FAKE")

It asserts, from the host side + inside the box, that (a) the box's egress is rewritten in flight —
the upstream sees the minted token, the box's dummy never reaches it; (b) the box trusts ONLY the
proxy's CA (the same request without it fails the TLS handshake — the CA mount is load-bearing); (c)
the 401 re-mint + re-issue (token rotation) reaches the box as a 200; and (d) the egress log records
it. The proxy daemon command + env come from the REAL ``ProxyPlugin.daemons``/``box_args`` — only a
fake minter + fake upstream stand in for GitHub. No GitHub App, no real token.

ADAPTED TOPOLOGY (see DEVELOPMENT.md "Nested-virtualization architecture"): the FAITHFUL path is
``foldyard host`` running the proxy + ``foldyard box up`` creating the box, with the box reaching the
proxy at ``FY_PROXY=host.containers.internal:8088`` — that needs foldyard to run as the *host* (a
Mac, or the nested-KVM "host" container), which the in-box guards + the absence of ``/dev/kvm``
forbid inside a dev box. Here the proxy + upstream run in THIS process and the box reaches them at
this container's own network IP (``FY_PROXY`` overridden to ``<self-ip>:<port>``). The ONLY thing
that differs from production is that proxy *address*; the CA mount, the proxy env, NO_PROXY, the
in-flight injection, and a real box container over the real socket are all the genuine article.

OPT-IN: skipped unless FOLDYARD_E2E=1, mitmdump is installed (the `e2e` group), the addon is in the
checkout, an engine is reachable, AND we can discover our own container's network (so a sibling box
can reach us). Run it from inside a container with engine access (the dev box or the nested host):

    just foldyard test-proxy-e2e        # runs -k "proxy_e2e or proxy_box_e2e" — both e2e files
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from e2e_box import BOX_IMAGE
from foldyard.plugins import InjectRule, Plugin, Registry
from foldyard.plugins.proxy import BOX_CA, ProxyPlugin

ADDON = Path(__file__).resolve().parents[1] / "src/foldyard/assets/proxy/egress_proxy.py"
# VM-visible scratch: bind-mount SOURCES resolve on the podman MACHINE, not in this container, so
# the CA the box mounts must live under the repo (which IS bind-mounted into the VM). tmp_path
# (container-local) is fine for everything only this process reads (cert/key/minter/log/confdir).
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
    """This container's (network, ipv4) — so a sibling box on the same network can reach the proxy
    + upstream we run in-process. None when not in a container with a usable network (e.g. a bare
    Mac host), which is the signal to skip: the adapted topology needs an engine-connected box."""
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
        if len(parts) == 2 and parts[1]:  # (name, ip) with a non-empty address
            return parts[0], parts[1]
    return None


_SELF = _self_on_network()

pytestmark = pytest.mark.skipif(
    os.environ.get("FOLDYARD_E2E") != "1"
    or shutil.which("mitmdump") is None
    or not ADDON.exists()
    or _SELF is None,
    reason=(
        "opt-in box+proxy e2e: set FOLDYARD_E2E=1, install the `e2e` group (mitmdump), have the "
        "addon, and run from a container with engine access (the dev box / nested host)"
    ),
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def _self_signed(cert: Path, key: Path, ip: str) -> None:
    """A throwaway upstream cert whose SAN is OUR ip — the proxy re-signs for that host with its
    own CA, and the box verifies against the (mounted) CA, so the upstream cert's trust is moot."""
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
    """A TLS echo upstream bound on all interfaces (so a sibling-reachable IP serves it): GET /echo
    → 200 echoing the Authorization header; GET /flap → 401 the FIRST time then 200 (rotation)."""
    state = {"flapped": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        # HTTP/1.1 + an explicit Content-Length so the box's curl sees a properly framed body and a
        # clean close — HTTP/1.0 read-to-EOF makes curl flag "unexpected eof" (error 56) over TLS.
        protocol_version = "HTTP/1.1"

        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            auth = self.headers.get("Authorization", "<none>")
            if self.path == "/flap" and not state["flapped"]:
                state["flapped"] = True
                self._reply(401, b"nope")
                return
            self._reply(200, auth.encode())

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


class _TestInjector(Plugin):
    """A consumer-style injector contributing ONE rule against our fake upstream — exactly how a
    real injector (github) feeds the proxy framework, so ``ProxyPlugin`` builds the daemon + box
    args for real. (The framework is what's under test; the minter + host are fakes.)"""

    name = "test-injector"

    def __init__(self, host: str, minter: str) -> None:
        self._host = host
        self._minter = minter

    def proxy_rules(self, mode: dict) -> list[InjectRule]:
        return [
            InjectRule(
                host=self._host,
                header="Authorization",
                minter=self._minter,
                replay_on_401=True,
                label="test injection proxy",
            )
        ]


class _Box:
    def __init__(self, engine: str, env: dict, name: str) -> None:
        self.engine, self.env, self.name = engine, env, name

    def exec(self, *cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.engine, "exec", self.name, *cmd],
            env=self.env, capture_output=True, text=True, timeout=60,
        )  # fmt: skip


@pytest.fixture
def box_proxy(tmp_path, monkeypatch):
    """Stand up: a fake minter + TLS upstream + a real mitmdump (driven by the REAL proxy-plugin
    daemon spec) in THIS process, and a real dev-box container brought up with the REAL
    ``registry().box_args`` proxy wiring. Yields (box, upstream-port, self-ip, egress-log)."""
    assert _SELF is not None  # guarded by pytestmark
    net, ip = _SELF
    engine, eng_env = _engine(), _engine_env()
    uport, pport = _free_port(), _free_port()

    cert, key = tmp_path / "up.crt", tmp_path / "up.key"
    _self_signed(cert, key, ip)
    httpd = _make_upstream(uport, cert, key)

    minter = tmp_path / "minter.py"
    minter.write_text("import json; print(json.dumps({'value': 'token FAKE', 'ttl': 3600}))\n")
    log = tmp_path / "egress.jsonl"
    confdir = tmp_path / "mitm"
    mitm_log = tmp_path / "mitmdump.log"

    # ── the proxy daemon, built from the REAL ProxyPlugin off a real InjectRule ──────────────
    reg = Registry([ProxyPlugin(), _TestInjector(ip, f"{sys.executable} {minter}")])
    spec = reg.desired_daemons({})["egress-proxy"]
    # The wiring under test: the daemon env is DERIVED FROM THE RULE (host/header/retry/minter),
    # not from egress_proxy.py's github-shaped defaults.
    # cmd[0] is resolved (foldyard's venv copy of mitmdump from the proxy extra, or PATH), so it
    # may be an absolute path — assert on the basename, not the bare name.
    assert os.path.basename(spec["cmd"][0]) == "mitmdump" and spec["cmd"][2].endswith(
        "egress_proxy.py"
    )
    assert spec["env"]["INJECT_HOST"] == ip
    assert spec["env"]["INJECT_HEADER"] == "Authorization"
    assert spec["env"]["INJECT_RETRY_401"] == "1"  # replay_on_401=True on the rule
    assert spec["env"]["INJECT_COMMAND"].endswith("minter.py")

    proc = subprocess.Popen(
        ["mitmdump", "-s", str(ADDON), "--listen-host", "0.0.0.0", "--listen-port", str(pport),
         "--set", f"confdir={confdir}", "--set", "ssl_insecure=true",
         "--set", "termlog_verbosity=info"],
        env={
            **os.environ,
            **spec["env"],  # the real rule-derived INJECT_* wiring
            "INJECT_RETRY_CA_BUNDLE": str(cert),  # verify the upstream against its self-signed cert
            "PROXY_LOG_FILE": str(log),  # override the daemon's config-dir log to our tmp
        },
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

        # The box mounts the CA via box_args' `-v` — whose SOURCE must be VM-visible, so copy the
        # CA under the repo (tmp_path is container-local; a sibling mount of it would be empty).
        vm_dir.mkdir(parents=True, exist_ok=True)
        vm_ca = vm_dir / "proxy-ca.pem"
        shutil.copyfile(ca, vm_ca)

        # ── the box run args, from the REAL registry().box_args (the surface under test) ───────
        # FY_PROXY is overridden to our reachable self-ip:port (the adapted-topology swap).
        # box_args reads the CA path from the ambient MITMPROXY_CA (a host-side var, not a derived
        # stack value) — point it at the VM-visible CA. Everything else (HTTPS_PROXY/NO_PROXY/
        # REQUESTS_CA_BUNDLE/the CA mount) is exactly what `foldyard box up` would inject.
        monkeypatch.setenv("MITMPROXY_CA", str(vm_ca))
        box_env = {"FY_PROXY": f"{ip}:{pport}", "PODMAN_PROJECT": "fyboxe2e"}
        plugin_args = reg.box_args(box_env)
        assert f"HTTPS_PROXY=http://{ip}:{pport}" in plugin_args  # proxy env baked in
        assert f"{vm_ca}:{BOX_CA}:ro" in plugin_args  # CA mounted in flight

        name = f"fy-box-e2e-{os.getpid()}"
        run = [
            engine, "run", "-d", "--name", name, "--network", net,
            "--security-opt", "label=disable", *plugin_args,
            BOX_IMAGE, "sleep", "infinity",
        ]  # fmt: skip
        created = subprocess.run(run, env=eng_env, capture_output=True, text=True, timeout=120)
        assert created.returncode == 0, f"box create failed:\n{created.stderr}"
        box = _Box(engine, eng_env, name)
        yield box, uport, ip, log
    finally:
        if box is not None:
            # `rm -f` is safe: the box is a plain `sleep infinity` (NO nested KVM guest — the only
            # thing the DEVELOPMENT.md teardown caution is about).
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


def test_box_egress_is_injected_through_the_proxy(box_proxy):
    box, uport, ip, log = box_proxy
    url = f"https://{ip}:{uport}/echo"

    # The box sends a DUMMY; the proxy overwrites it in flight, so the upstream echoes the minted
    # token. curl picks the proxy up from $https_proxy (box_args set it) + trusts the mounted CA.
    got = box.exec("curl", "-sS", "--cacert", str(BOX_CA), "-H", "Authorization: Bearer DUMMY", url)
    assert got.returncode == 0, f"curl through the proxy failed:\n{got.stdout}\n{got.stderr}"
    assert got.stdout == "token FAKE", (
        f"box egress not injected; got {got.stdout!r} / {got.stderr!r}"
    )

    # Host-side: the egress log recorded the request as injected for the target host.
    _wait(
        lambda: any(e.get("injected") and e.get("host") == ip for e in _log_entries(log)),
        5,
        "an injected entry in the egress log",
        log.read_text() if log.exists() else "",
    )


def test_box_ca_mount_is_load_bearing(box_proxy):
    box, uport, ip, _ = box_proxy
    # The SAME request without trusting the proxy CA must fail the TLS handshake — proving the CA
    # box_args mounts is what makes egress work (not some ambient trust in the box image).
    got = box.exec("curl", "-sS", f"https://{ip}:{uport}/echo")  # default (system) trust store
    assert got.returncode != 0, f"expected a TLS failure without the CA, got 0:\n{got.stdout}"


def test_box_401_rotation_reaches_the_box_as_200(box_proxy):
    box, uport, ip, log = box_proxy
    # /flap 401s once; the proxy re-mints + re-issues and hands the box the retried 200 (the real
    # token-rotation path, through a real box). Proves the fix end to end from the box's side.
    got = box.exec("curl", "-sS", "--cacert", str(BOX_CA), f"https://{ip}:{uport}/flap")
    assert got.returncode == 0, f"rotation curl failed:\n{got.stdout}\n{got.stderr}"
    assert got.stdout == "token FAKE", f"box did not get the retried token; got {got.stdout!r}"
    _wait(
        lambda: any(
            e.get("replayed") and e.get("status") == 200 and e.get("host") == ip
            for e in _log_entries(log)
        ),
        5,
        "a replayed 200 in the egress log",
        log.read_text() if log.exists() else "",
    )
