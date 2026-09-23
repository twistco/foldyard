"""Opt-in LIVE e2e for the proxy's live settings — on a real mitmdump, the addon's own process.

The unit tier (test_proxy_inject.py) drives the addon against a stubbed mitmproxy. Two things it
can't show are what this file is for:

  - a posture change no longer costs what's in flight: a download streaming through the proxy
    finishes while its settings change underneath, on the same process (the regression: a mode
    switch restarted mitmdump and cut an apt download mid-package);
  - a narrowing still closes what it no longer allows. A blind tunnel has no flow, and mitmproxy
    has no public API to close a connection without one, so the addon reaches its `proxyserver`
    addon's connection table. That internal is what this pins, against the mitmproxy we ship.

OPT-IN like test_proxy_e2e.py: FOLDYARD_E2E=1 and mitmdump installed (`just test-proxy-e2e`).
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import pytest

from test_proxy_e2e import ADDON, HOST, _free_port, _port_open, _self_signed, _wait
from test_proxy_e2e import pytestmark as _e2e_gate

pytestmark = _e2e_gate

_SLOW_CHUNKS = 30  # × 0.1 s: a download still in flight when the settings change
_CHUNK = b"x" * 4096


def _upstream(port: int, cert: Path, key: Path) -> http.server.ThreadingHTTPServer:
    """TLS upstream: /echo answers at once (keep-alive), /slow drips a body over ~3 s."""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive, so one tunnel can carry several requests

        def do_GET(self):
            if self.path == "/slow":
                self.send_response(200)
                self.send_header("Content-Length", str(_SLOW_CHUNKS * len(_CHUNK)))
                self.end_headers()
                for _ in range(_SLOW_CHUNKS):
                    self.wfile.write(_CHUNK)
                    self.wfile.flush()
                    time.sleep(0.1)
                return
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    httpd = http.server.ThreadingHTTPServer((HOST, port), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certfile=str(cert), keyfile=str(key))
    httpd.socket = tls.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _write_live(path: Path, **settings) -> None:
    data = {"rules": [], "default_deny": False, "passthrough": [], **settings}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)  # the supervisor's write: whole, renamed into place


@pytest.fixture
def rig(tmp_path):
    uport, pport = _free_port(), _free_port()
    cert, key = tmp_path / "up.crt", tmp_path / "up.key"
    _self_signed(cert, key)
    httpd = _upstream(uport, cert, key)
    live = tmp_path / "proxy-live.json"
    _write_live(live)
    confdir, mitm_log = tmp_path / "mitm", tmp_path / "mitmdump.log"
    proc = subprocess.Popen(
        ["mitmdump", "-s", str(ADDON), "--listen-host", HOST, "--listen-port", str(pport),
         "--set", f"confdir={confdir}", "--set", "ssl_insecure=true",
         "--set", "stream_large_bodies=1m", "--set", "termlog_verbosity=info"],
        env={**os.environ, "PYTHONUNBUFFERED": "1", "LIVE_FILE": str(live),
             "PROXY_LOG_FILE": str(tmp_path / "e.jsonl")},
        stdout=mitm_log.open("w"), stderr=subprocess.STDOUT,
    )  # fmt: skip

    def diag() -> str:
        return mitm_log.read_text() if mitm_log.exists() else "(no mitmdump output)"

    try:
        _wait(lambda: _port_open(pport), 30, "mitmdump to listen", diag())
        _wait((confdir / "mitmproxy-ca-cert.pem").exists, 30, "the mitm CA", diag())
        yield {
            "proc": proc, "live": live, "uport": uport, "pport": pport, "cert": cert,
            "ca": confdir / "mitmproxy-ca-cert.pem", "diag": diag,
        }  # fmt: skip
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        httpd.shutdown()


def _tunnel(rig, trust: Path) -> ssl.SSLSocket:
    """A raw CONNECT through the proxy, then TLS trusting ``trust`` — the upstream's own cert
    proves the proxy tunnelled blind; the mitm CA would prove it decrypted."""
    raw = socket.create_connection((HOST, rig["pport"]), timeout=5)
    raw.sendall(f"CONNECT {HOST}:{rig['uport']} HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    reply = b""
    while b"\r\n\r\n" not in reply:
        reply += raw.recv(4096)
    assert reply.startswith(b"HTTP/1.1 200"), reply
    tls = ssl.create_default_context(cafile=str(trust))
    tls.check_hostname = False
    return tls.wrap_socket(raw, server_hostname=HOST)


def _get(sock: ssl.SSLSocket, path: str = "/echo") -> bytes:
    """One keep-alive request on the tunnel; the whole response (headers + the 2-byte body)."""
    sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    got = b""
    while not got.endswith(b"\r\n\r\nok"):
        chunk = sock.recv(4096)
        if not chunk:
            break
        got += chunk
    return got


def _closed(sock: ssl.SSLSocket) -> bool:
    sock.settimeout(0.2)
    try:
        return sock.recv(1) == b""
    except TimeoutError:
        return False
    except (OSError, ssl.SSLError):
        return True


def test_a_download_in_flight_survives_a_settings_change(rig):
    import requests

    pid = rig["proc"].pid
    result: dict = {}

    def download() -> None:
        session = requests.Session()
        session.trust_env = False
        got = session.get(
            f"https://{HOST}:{rig['uport']}/slow",
            proxies={"https": f"http://{HOST}:{rig['pport']}"},
            verify=str(rig["ca"]),  # decrypted, like the box's traffic
            timeout=30,
        )
        result["status"], result["size"] = got.status_code, len(got.content)

    worker = threading.Thread(target=download)
    worker.start()
    time.sleep(0.8)  # well into the body
    _write_live(
        rig["live"],
        rules=[{"host": "api.example.test", "command": "true"}],
        passthrough=["files.example.test"],
    )
    worker.join(timeout=30)
    assert result == {"status": 200, "size": _SLOW_CHUNKS * len(_CHUNK)}, rig["diag"]()
    assert rig["proc"].poll() is None and rig["proc"].pid == pid  # the same process throughout
    _wait(lambda: "settings reloaded" in rig["diag"](), 5, "the reload log line", rig["diag"]())


def test_a_tunnel_whose_host_leaves_passthrough_is_closed(rig):
    _write_live(rig["live"], passthrough=[HOST])
    time.sleep(1.5)  # the addon's poll
    tunnel = _tunnel(rig, trust=rig["cert"])  # the REAL cert: tunnelled, not decrypted
    assert _get(tunnel).endswith(b"ok")

    _write_live(rig["live"], passthrough=[])  # the proxy would decrypt this host now
    _wait(lambda: _closed(tunnel), 5, "the tunnel to be closed", rig["diag"]())
    assert rig["proc"].poll() is None  # closed one connection, not the proxy
    # mitmdump's output to a file is block-buffered: wait for the line rather than read it once.
    _wait(lambda: "closed the connection" in rig["diag"](), 10, "the close log line", rig["diag"]())


def test_a_tunnel_the_change_does_not_touch_stays_open(rig):
    _write_live(rig["live"], passthrough=[HOST])
    time.sleep(1.5)
    tunnel = _tunnel(rig, trust=rig["cert"])
    assert _get(tunnel).endswith(b"ok")
    _write_live(rig["live"], passthrough=[HOST, "other.example.test"])  # widening
    time.sleep(2.5)
    assert not _closed(tunnel)
    tunnel.settimeout(5)
    assert _get(tunnel).endswith(b"ok")
