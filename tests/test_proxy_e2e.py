"""Opt-in LIVE egress-proxy e2e — the WHOLE machinery on the wire, apart from a real token.

This stands up the real stack the `proxy` plugin would run and drives a request through it:

    a "box" client ──HTTPS via proxy, trusting the mitm CA──▶ mitmdump + egress_proxy.py ──▶ upstream
       (REQUESTS_CA_BUNDLE = the CA, exactly         (a fake minter mints "token FAKE";          (echoes the
        like the box's box_args)                      egress_proxy OVERWRITES Authorization)          header it got)

and then checks, FROM THE HOST SIDE, that (a) the upstream received the REWRITTEN header — so the
injection happened in flight and the box's dummy never reached it, (b) the egress JSONL network
log recorded the request as injected, and (c) the HTTPS handshake only succeeded because the
client trusted the proxy's CA. A second case exercises the 401 re-mint + re-issue (token rotation):
the client gets the retried 200, proving the modern-mitmproxy idiom (the old `replay.client` of a
live flow is rejected by 12.x). No GitHub App, no real token, no dev box VM — just the proxy + a
fake minter + a CA-trusting client.

OPT-IN: skipped unless FOLDYARD_E2E=1 AND mitmdump is installed (the `e2e` dep group). Run it:

    just foldyard test-proxy-e2e

The egress_proxy addon is still consumer-side; skip cleanly if it isn't in the checkout.
"""

from __future__ import annotations

import http.server
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

ADDON = Path(__file__).resolve().parents[1] / "src/foldyard/assets/proxy/egress_proxy.py"

pytestmark = pytest.mark.skipif(
    os.environ.get("FOLDYARD_E2E") != "1" or shutil.which("mitmdump") is None or not ADDON.exists(),
    reason="opt-in proxy e2e: set FOLDYARD_E2E=1, install the `e2e` group (mitmdump), have the addon",
)

HOST = "127.0.0.1"  # also the injected host (pretty_host of a request to this loopback addr)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _self_signed(cert: Path, key: Path) -> None:
    """A throwaway self-signed cert for the upstream. mitmdump connects to it with
    ssl_insecure=true, so its trust/SAN don't matter — it just needs to be a valid pair."""
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
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(HOST))]),
            critical=False,
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
    """A TLS echo upstream: GET /echo → 200 echoing the Authorization header it received;
    GET /flap → 401 the FIRST time then 200 (to exercise the proxy's 401 re-mint + re-issue)."""

    state = {"flapped": False}  # closure state (no dynamic attr on the server object)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            auth = self.headers.get("Authorization", "<none>")
            if self.path == "/flap" and not state["flapped"]:
                state["flapped"] = True
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"nope")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(auth.encode())

        def log_message(self, format, *args):  # match the base signature; keep output clean
            pass

    httpd = http.server.ThreadingHTTPServer((HOST, port), Handler)
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


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, port)) == 0


class _Proxy:
    def __init__(self, uport, pport, ca, log, mitm_log):
        self.uport, self.pport, self.ca, self.log, self.mitm_log = uport, pport, ca, log, mitm_log

    def get(self, path: str, auth: str = "Bearer DUMMY", timeout: float = 20):
        """The 'box': an HTTPS request through the proxy, trusting ONLY the mitm CA."""
        import requests

        s = requests.Session()
        s.trust_env = False  # ignore ambient proxy/no_proxy; route 127.0.0.1 via our proxy
        return s.get(
            f"https://{HOST}:{self.uport}{path}",
            proxies={"https": f"http://{HOST}:{self.pport}"},
            verify=str(self.ca),  # == the box's REQUESTS_CA_BUNDLE
            headers={"Authorization": auth},
            timeout=timeout,
        )


@pytest.fixture
def proxy(tmp_path):
    """Stand up the upstream + a real mitmdump running egress_proxy.py with a fake minter, and
    tear both down. Yields a _Proxy handle (ports, the CA path, the egress log path)."""
    uport, pport = _free_port(), _free_port()
    cert, key = tmp_path / "up.crt", tmp_path / "up.key"
    _self_signed(cert, key)
    httpd = _make_upstream(uport, cert, key)

    minter = tmp_path / "minter.py"
    minter.write_text("import json; print(json.dumps({'value': 'token FAKE', 'ttl': 3600}))\n")
    log = tmp_path / "egress.jsonl"
    confdir = tmp_path / "mitm"
    mitm_log = tmp_path / "mitmdump.log"

    cmd = [
        "mitmdump", "-s", str(ADDON),
        "--listen-host", HOST, "--listen-port", str(pport),
        "--set", f"confdir={confdir}", "--set", "ssl_insecure=true",
        "--set", "termlog_verbosity=info",
    ]  # fmt: skip
    proc = subprocess.Popen(
        cmd,
        env={
            **os.environ,
            "INJECT_HOST": HOST,
            "INJECT_COMMAND": f"{sys.executable} {minter}",
            "INJECT_HEADER": "Authorization",
            "INJECT_RETRY_401": "1",
            # The 401 re-issue verifies the upstream against its (self-signed) cert — TLS stays on,
            # just pinned to the test's CA instead of the system store (api.github.com in prod).
            "INJECT_RETRY_CA_BUNDLE": str(cert),
            "PROXY_LOG_FILE": str(log),
        },
        stdout=mitm_log.open("w"),
        stderr=subprocess.STDOUT,
    )
    ca = confdir / "mitmproxy-ca-cert.pem"

    def diag() -> str:
        return mitm_log.read_text() if mitm_log.exists() else "(no mitmdump output)"

    try:
        _wait(lambda: _port_open(pport), 30, "mitmdump to listen", diag())
        _wait(ca.exists, 30, "the mitm CA to be written", diag())
        yield _Proxy(uport, pport, ca, log, mitm_log)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        httpd.shutdown()


def _log_entries(log: Path) -> list[dict]:
    import json

    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_proxy_rewrites_header_and_logs_while_box_trusts_the_ca(proxy):
    # The box sends a DUMMY; the proxy overwrites it in flight, so the upstream sees the minted
    # token. The HTTPS handshake succeeds ONLY because the client trusts the proxy's CA.
    resp = proxy.get("/echo", auth="Bearer DUMMY")
    assert resp.status_code == 200
    assert resp.text == "token FAKE", (
        f"upstream saw {resp.text!r}; mitmdump:\n{proxy.mitm_log.read_text()}"
    )

    # Host-side: the egress log recorded the request, flagged injected for the target host.
    def _injected_entry():
        return any(e.get("injected") and e.get("host") == HOST for e in _log_entries(proxy.log))

    _wait(
        _injected_entry,
        5,
        "an injected entry in the egress log",
        proxy.log.read_text() if proxy.log.exists() else "",
    )


def test_ca_trust_is_load_bearing(proxy):
    # The same request WITHOUT trusting the CA must fail the TLS handshake — proving the CA is
    # what makes it work (the box "being happy with the CA" is a real, checked property).
    import requests

    s = requests.Session()
    s.trust_env = False
    with pytest.raises(requests.exceptions.SSLError):
        s.get(
            f"https://{HOST}:{proxy.uport}/echo",
            proxies={"https": f"http://{HOST}:{proxy.pport}"},
            verify=True,  # the system trust store does NOT include the throwaway mitm CA
            headers={"Authorization": "Bearer DUMMY"},
            timeout=20,
        )


def test_proxy_re_issues_on_401_and_returns_200(proxy):
    # /flap 401s the FIRST time, then 200s. The proxy must re-mint + re-issue with the fresh token
    # and hand the retried 200 back to the waiting client — the real-world token-rotation path.
    #
    # This is the modern-mitmproxy idiom: a direct re-issue whose response overwrites flow.response.
    # The old approach (`replay.client` of the live flow) is rejected by mitmproxy 12.x with "Can't
    # replay live flow" — which this very e2e surfaced — so it would hang the client instead.
    resp = proxy.get("/flap", auth="Bearer DUMMY", timeout=20)
    assert resp.status_code == 200, (
        f"client never got the retried 200\n{proxy.mitm_log.read_text()}"
    )
    # The upstream echoes the Authorization it finally saw → the body is the minted token, proving
    # the re-issue carried it (not the box's dummy).
    assert resp.text == "token FAKE", (
        f"upstream saw {resp.text!r}; mitmdump:\n{proxy.mitm_log.read_text()}"
    )

    # Host-side: the egress log records it as a REPLAYED 200 on the injected host (one entry — the
    # 401 is absorbed into the retry, so the log shows the final, successful state).
    def _replayed_200():
        return any(
            e.get("replayed")
            and e.get("status") == 200
            and e.get("host") == HOST
            and e.get("injected")
            for e in _log_entries(proxy.log)
        )

    _wait(
        _replayed_200,
        5,
        "a replayed 200 in the egress log",
        proxy.log.read_text() if proxy.log.exists() else "",
    )
