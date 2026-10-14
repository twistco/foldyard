"""Opt-in LIVE e2e for the wall on a real mitmdump: a refused host is never dialled.

The proxy answers a refused host's CONNECT itself and decrypts the tunnel, so the 403 reaches the
request inside and the egress log names its method and path. That is only safe if no upstream
connection is ever opened for it, and three of mitmproxy's own mechanisms decide that: its eager
dial on CONNECT (skipped when the CONNECT already has a response), the server-first TLS handshake
it asks for on the ClientHello (turned off for that connection), and the `server_connect` hook
the addon refuses an ungranted dial in, whatever layer asks. The unit tier drives stub flows and
cannot see a socket open; this counts the connections a refused :443 listener accepts.

OPT-IN like test_proxy_e2e.py: FOLDYARD_E2E=1 and mitmdump installed (`just test-proxy-e2e`).
The refused listener needs :443 on a second loopback address — skipped where it can't be bound
(no `127.0.0.2` on macOS; an unprivileged user below `ip_unprivileged_port_start`).
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time

import pytest

from test_proxy_e2e import HOST, _log_entries, _running_proxy, _wait
from test_proxy_e2e import pytestmark as _e2e_gate

pytestmark = _e2e_gate

REFUSED = "127.0.0.2"  # a host no grant covers, listening on :443


class _Counter:
    """A TCP listener on REFUSED:443 that only counts the connections it accepts."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((REFUSED, 443))
        except OSError as e:
            self.sock.close()
            pytest.skip(f"can't listen on {REFUSED}:443 here ({e})")
        self.sock.listen()
        self.accepted = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            conn.close()

    def close(self) -> None:
        # close() alone leaves the accept() blocked in the thread holding :443 listening;
        # shutdown wakes it.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


@pytest.fixture
def wall(tmp_path):
    counter = _Counter()
    allow = tmp_path / "allow-effective.json"
    rig = tmp_path / "rig"
    rig.mkdir()
    try:
        with _running_proxy(
            rig,
            "import json; print(json.dumps({'value': 'token FAKE', 'ttl': 3600}))\n",
            env={"DEFAULT_DENY": "1", "ALLOW_FILE": str(allow)},
        ) as proxy:
            # The echo upstream is granted by port (it isn't on :443); REFUSED is not.
            allow.write_text(json.dumps({"default_deny": True, "allow": [f"{HOST}:{proxy.uport}"]}))
            yield proxy, counter
    finally:
        counter.close()


def _session():
    import requests

    s = requests.Session()
    s.trust_env = False
    return s


def _rows_for(proxy, host: str) -> list[dict]:
    return [r for r in _log_entries(proxy.log) if r.get("host") == host and r.get("blocked")]


def _settle() -> None:
    time.sleep(0.5)  # a dial, had one been attempted, has had its chance to land


def test_a_refused_https_request_is_answered_inside_and_never_dialled(wall):
    proxy, counter = wall
    resp = _session().post(
        f"https://{REFUSED}/v1/upload?x=1",
        proxies={"https": f"http://{HOST}:{proxy.pport}"},
        verify=str(proxy.ca),
        headers={"User-Agent": "probe/1.0"},
        data=b"payload",
        timeout=20,
    )
    assert resp.status_code == 403 and "fy allow add" in resp.text
    _wait(lambda: _rows_for(proxy, REFUSED), 5, "the blocked row", proxy.mitm_log.read_text())
    (row,) = _rows_for(proxy, REFUSED)
    assert (row["method"], row["path"], row["ua"]) == ("POST", "/v1/upload?x=1", "probe/1.0")
    _settle()
    assert counter.accepted == 0, proxy.mitm_log.read_text()

    # The granted upstream still goes out, dialled eagerly as before.
    ok = proxy.get("/echo")
    assert ok.status_code == 200


def test_a_client_without_the_ca_fails_tls_and_gets_the_connect_row(wall):
    proxy, counter = wall
    import requests

    with pytest.raises(requests.exceptions.SSLError):
        _session().get(
            f"https://{REFUSED}/",
            proxies={"https": f"http://{HOST}:{proxy.pport}"},
            verify=True,  # the system store: no proxy CA in it
            timeout=20,
        )
    _wait(lambda: _rows_for(proxy, REFUSED), 5, "the CONNECT row", proxy.mitm_log.read_text())
    (row,) = _rows_for(proxy, REFUSED)
    assert (row["method"], row["path"]) == ("CONNECT", "")
    _settle()
    assert counter.accepted == 0, proxy.mitm_log.read_text()


def test_raw_tcp_through_an_answered_tunnel_is_never_dialled(wall):
    # Past the 200, a client that speaks no TLS reaches mitmproxy's raw TCP layer, which DOES
    # dial. The server_connect hook is what refuses it.
    proxy, counter = wall
    with socket.create_connection((HOST, proxy.pport), timeout=10) as s:
        s.sendall(f"CONNECT {REFUSED}:443 HTTP/1.1\r\nHost: {REFUSED}:443\r\n\r\n".encode())
        head = s.recv(4096)
        assert head.startswith(b"HTTP/1.1 200"), head
        s.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
        s.settimeout(5)
        try:
            while s.recv(4096):
                pass
        except (TimeoutError, ConnectionResetError):
            pass
    _wait(lambda: _rows_for(proxy, REFUSED), 5, "the CONNECT row", proxy.mitm_log.read_text())
    _settle()
    assert counter.accepted == 0, proxy.mitm_log.read_text()


def test_a_non_tls_port_is_still_refused_at_connect(wall):
    # No request to read in a non-TLS tunnel: :22 keeps the readable 403 on the CONNECT itself.
    proxy, _counter = wall
    with socket.create_connection((HOST, proxy.pport), timeout=10) as s:
        s.sendall(f"CONNECT {REFUSED}:22 HTTP/1.1\r\nHost: {REFUSED}:22\r\n\r\n".encode())
        assert s.recv(4096).startswith(b"HTTP/1.1 403")
    _wait(lambda: _rows_for(proxy, f"{REFUSED}:22"), 5, "the row", proxy.mitm_log.read_text())


def test_tls_to_the_answered_tunnel_completes_without_an_upstream(wall):
    # The handshake itself: with the eager strategy mitmproxy would first handshake the upstream
    # (and fail, or dial). Here it completes against the proxy's own certificate for the host.
    proxy, counter = wall
    ctx = ssl.create_default_context(cafile=str(proxy.ca))
    with socket.create_connection((HOST, proxy.pport), timeout=10) as raw:
        raw.sendall(f"CONNECT {REFUSED}:443 HTTP/1.1\r\nHost: {REFUSED}:443\r\n\r\n".encode())
        assert raw.recv(4096).startswith(b"HTTP/1.1 200")
        with ctx.wrap_socket(raw, server_hostname=REFUSED) as tls:
            assert tls.version() is not None
    _settle()
    assert counter.accepted == 0, proxy.mitm_log.read_text()
