"""Opt-in LIVE e2e: a real mitmdump never dials a local address no grant names.

The proxy runs on your computer, so a dial it makes to loopback reaches a service there that was
never offered to the box. The unit tier drives the hooks and a bare asyncio loop; this runs the
addon inside mitmdump and counts what a loopback listener accepts: nothing for an address, nothing
for a NAME that resolves there (`localhost`, through the resolver guard — the proof that the dial
mitmproxy makes is the one the guard vets), and only after a grant naming it.

Observing, not enforcing: the wall's mode must not open the door — the learn window used to let the
box reach every service on the operator's loopback. OPT-IN like test_proxy_e2e.py.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time

import pytest

from test_proxy_e2e import HOST, _free_port, _log_entries, _running_proxy
from test_proxy_e2e import pytestmark as _e2e_gate

pytestmark = _e2e_gate


class _Service:
    """A plain-HTTP service on loopback — the dev server nobody offered the box — counting the
    requests it answers."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.hits = 0
        service = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                service.hits += 1
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer((HOST, self.port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def rig(tmp_path):
    service = _Service()
    try:
        with _running_proxy(
            tmp_path, "import json; print(json.dumps({'value': 'x', 'ttl': 60}))\n"
        ) as proxy:
            yield proxy, service, tmp_path / "allow-effective.json"
    finally:
        service.close()


def _get(proxy, url: str):
    import requests

    s = requests.Session()
    s.trust_env = False  # route loopback through the proxy, as a box client sent there would be
    return s.get(url, proxies={"http": f"http://{HOST}:{proxy.pport}"}, timeout=20)


def _grant(allow, proxy, *keys: str) -> None:
    allow.write_text(json.dumps({"allow": [f"{HOST}:{proxy.uport}", *keys]}))
    time.sleep(1.1)  # the proxy re-reads ALLOW_FILE on an mtime change; cross a 1 s granularity


@pytest.mark.parametrize("host", [HOST, "localhost"])
def test_a_local_service_is_never_reached_without_a_grant(rig, host):
    proxy, service, _allow = rig
    resp = _get(proxy, f"http://{host}:{service.port}/")
    assert resp.status_code == 502, resp.text
    assert "a local address" in resp.text and f"fy allow add {HOST}:{service.port}" in resp.text
    assert service.hits == 0
    rows = [r for r in _log_entries(proxy.log) if r.get("local")]
    assert rows and rows[-1]["host"] == f"{HOST}:{service.port}"


def test_a_raw_connect_to_it_is_never_dialled_either(rig):
    proxy, service, _allow = rig
    with socket.create_connection((HOST, proxy.pport), timeout=10) as raw:
        raw.sendall(f"CONNECT localhost:{service.port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        status = raw.recv(4096).split(b"\r\n", 1)[0]
    assert b" 200 " not in status, status
    assert service.hits == 0


@pytest.mark.parametrize("host", [HOST, "localhost"])
def test_a_grant_naming_the_address_lets_it_through(rig, host):
    # `localhost` resolves to ::1 as well, which can't be granted: the guard drops that answer
    # and the dial goes to the granted 127.0.0.1.
    proxy, service, allow = rig
    _grant(allow, proxy, f"{HOST}:{service.port}")
    resp = _get(proxy, f"http://{host}:{service.port}/")
    assert (resp.status_code, resp.text) == (200, "ok")
    assert service.hits == 1
