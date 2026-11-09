"""Opt-in LIVE e2e for the wall on a real mitmdump: a refused host is never dialled.

The proxy answers a refused host's CONNECT itself and decrypts the tunnel, so the 403 reaches the
request inside and the egress log names its method and path. That is only safe if no upstream
connection is ever opened for it, and three of mitmproxy's own mechanisms decide that: its eager
dial on CONNECT (skipped when the CONNECT already has a response), the server-first TLS handshake
it asks for on the ClientHello (still attempted — no addon can turn it off), and the
`server_connect` hook the addon refuses an ungranted dial in, whatever layer asks — that
handshake's included. The unit tier drives stub flows and cannot see a socket open; this counts
the connections a refused :443 listener accepts.

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
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((HOST, proxy.pport), timeout=10) as raw:
        raw.sendall(f"CONNECT {REFUSED}:443 HTTP/1.1\r\nHost: {REFUSED}:443\r\n\r\n".encode())
        assert raw.recv(4096).startswith(b"HTTP/1.1 200")
        with ctx.wrap_socket(raw, server_hostname=REFUSED) as tls:
            assert tls.version() is not None
    _settle()
    assert counter.accepted == 0, proxy.mitm_log.read_text()


# ── path grants on the wire ───────────────────────────────────────────────────────────────────
# A host granted only for paths has its CONNECT answered like a refused one, so the unit tier
# can't see the part that matters: that mitmproxy then dials it for the ClientHello (the eager
# handshake) and carries the granted read over that connection, while a raw TCP tunnel's dial is
# refused and raw bytes after a decrypted handshake (which mitmproxy relays, not parses, when they
# don't look like HTTP) never reach the host.

SCOPED = "127.0.0.3"  # granted for one path, listening on :443
PREFIX = "/bucket/"


class _Recorder:
    """A TLS listener on SCOPED:443 that records every connection's bytes and answers a GET."""

    def __init__(self, cert, key) -> None:
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((SCOPED, 443))
        except OSError as e:
            self.sock.close()
            pytest.skip(f"can't listen on {SCOPED}:443 here ({e})")
        self.sock.listen()
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        # mitmproxy offers upstream what the box offered it, and answers the box with what the
        # upstream chose: HTTP/2 to the box only where the host speaks it, as GCS does.
        self.ctx.set_alpn_protocols(["h2", "http/1.1"])
        self.accepted = 0
        self.received: list[bytes] = []
        # What came after the answer: a body the request head didn't announce (mitmproxy relays
        # an HTTP/2 request's DATA as raw bytes after an HTTP/1 head) arrives here.
        self.after: list[bytes] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn) -> None:
        try:
            tls = self.ctx.wrap_socket(conn, server_side=True)
            if tls.selected_alpn_protocol() == "h2":
                self._h2(tls)
                return
            tls.settimeout(3)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.received.append(data)
            if data.startswith((b"GET ", b"HEAD ")):
                path = data.split(b" ", 2)[1]
                body = b"ok " + path
                tls.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                    % (len(body), body)
                )
                tls.settimeout(1)
                after = b""
                try:
                    while chunk := tls.recv(4096):
                        after += chunk
                except (TimeoutError, OSError):
                    pass
                self.after.append(after)
            tls.close()
        except (OSError, ssl.SSLError):
            pass
        finally:
            conn.close()

    def _h2(self, tls) -> None:
        """An HTTP/2 connection: each request's head goes to ``received`` (as ``METHOD path``),
        every DATA chunk and trailer field to ``after`` as it arrives (the connection may still be
        open when a test looks), and every stream that ends is answered 200."""
        import h2.config
        import h2.connection
        import h2.events
        import h2.exceptions

        conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
        conn.initiate_connection()
        tls.sendall(conn.data_to_send())
        tls.settimeout(5)
        try:
            while data := tls.recv(65535):
                for event in conn.receive_data(data):
                    if isinstance(event, h2.events.RequestReceived):
                        head = dict(event.headers)
                        self.received.append(head[b":method"] + b" " + head[b":path"])
                    elif isinstance(event, h2.events.DataReceived):
                        self.after.append(event.data)
                        conn.acknowledge_received_data(
                            event.flow_controlled_length, event.stream_id
                        )
                    elif isinstance(event, h2.events.StreamEnded):
                        conn.send_headers(
                            event.stream_id, [(":status", "200"), ("content-length", "2")]
                        )
                        conn.send_data(event.stream_id, b"ok", end_stream=True)
                    elif isinstance(event, h2.events.TrailersReceived):
                        self.after.extend(b"%s: %s" % field for field in event.headers)
                tls.sendall(conn.data_to_send())
        except (OSError, ssl.SSLError, h2.exceptions.H2Error):
            pass

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


@pytest.fixture
def scoped(tmp_path):
    from test_proxy_e2e import _self_signed

    cert, key = tmp_path / "scoped.crt", tmp_path / "scoped.key"
    _self_signed(cert, key)
    recorder = _Recorder(cert, key)
    allow = tmp_path / "allow-effective.json"
    rig = tmp_path / "rig"
    rig.mkdir()
    try:
        with _running_proxy(
            rig,
            "import json; print(json.dumps({'value': 'token FAKE', 'ttl': 3600}))\n",
            env={"DEFAULT_DENY": "1", "ALLOW_FILE": str(allow)},
        ) as proxy:
            allow.write_text(json.dumps({"default_deny": True, "allow": [f"{SCOPED}{PREFIX}"]}))
            yield proxy, recorder
    finally:
        recorder.close()


def _via(proxy, method: str, path: str, **kw):
    return _session().request(
        method,
        f"https://{SCOPED}{path}",
        proxies={"https": f"http://{HOST}:{proxy.pport}"},
        verify=str(proxy.ca),
        timeout=20,
        **kw,
    )


def test_a_granted_read_reaches_the_host_through_the_answered_tunnel(scoped):
    proxy, recorder = scoped
    resp = _via(proxy, "GET", f"{PREFIX}chrome.zip?generation=1")
    assert resp.status_code == 200, proxy.mitm_log.read_text()
    assert resp.text == f"ok {PREFIX}chrome.zip?generation=1"
    assert any(r.startswith(f"GET {PREFIX}chrome.zip".encode()) for r in recorder.received)


def test_outside_the_prefix_or_not_a_read_never_reaches_the_host(scoped):
    proxy, recorder = scoped
    for method, path, data in (
        ("GET", "/other/secret", None),
        ("GET", f"{PREFIX}../other/secret", None),
        ("PUT", f"{PREFIX}upload", b"payload"),
        ("POST", f"{PREFIX}upload", b"payload"),
    ):
        resp = _via(proxy, method, path, data=data)
        assert resp.status_code == 403, (method, path)
        assert f"{SCOPED}{PREFIX}" in resp.text
    _settle()
    # The ClientHello may have handshaken the host; no request line ever reached it.
    assert all(not r for r in recorder.received), recorder.received
    rows = _rows_for(proxy, SCOPED)
    assert {(r["method"], r.get("outside")) for r in rows} == {
        ("GET", True),
        ("PUT", True),
        ("POST", True),
    }


def test_a_raw_tcp_tunnel_to_a_path_host_is_never_dialled(scoped):
    proxy, recorder = scoped
    with socket.create_connection((HOST, proxy.pport), timeout=10) as s:
        s.sendall(f"CONNECT {SCOPED}:443 HTTP/1.1\r\nHost: {SCOPED}:443\r\n\r\n".encode())
        assert s.recv(4096).startswith(b"HTTP/1.1 200")
        # Not TLS, and not HTTP to mitmproxy (a leading CRLF): its raw TCP layer, which dials.
        s.sendall(b"\r\nPUT /other/x HTTP/1.1\r\nHost: x\r\n\r\n")
        s.settimeout(5)
        try:
            while s.recv(4096):
                pass
        except (TimeoutError, ConnectionResetError):
            pass
    _settle()
    assert recorder.accepted == 0, proxy.mitm_log.read_text()


def test_raw_bytes_after_a_decrypted_handshake_never_reach_the_host(scoped):
    # A leading CRLF makes mitmproxy relay the stream raw instead of parsing it as HTTP — and a
    # server may skip that CRLF and read the request after it.
    proxy, recorder = scoped
    ctx = ssl.create_default_context(cafile=str(proxy.ca))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((HOST, proxy.pport), timeout=10) as raw:
        raw.sendall(f"CONNECT {SCOPED}:443 HTTP/1.1\r\nHost: {SCOPED}:443\r\n\r\n".encode())
        assert raw.recv(4096).startswith(b"HTTP/1.1 200")
        with ctx.wrap_socket(raw, server_hostname=SCOPED) as tls:
            tls.sendall(b"\r\nPUT /other/x HTTP/1.1\r\nHost: " + SCOPED.encode() + b"\r\n\r\n")
            tls.settimeout(5)
            try:
                while tls.recv(4096):
                    pass
            except (TimeoutError, ConnectionResetError, ssl.SSLError):
                pass
    _settle()
    assert all(b"PUT" not in r for r in recorder.received), recorder.received
    _wait(
        lambda: any(r["method"] == "TCP" for r in _rows_for(proxy, SCOPED)),
        5,
        "the raw-bytes refusal row",
        proxy.mitm_log.read_text(),
    )


def test_a_body_an_http2_read_doesnt_announce_never_reaches_the_host(scoped):
    # HTTP/2 lets a GET leave its stream open after HEADERS and send DATA with no content-length,
    # so the headers give the wall nothing to refuse — and mitmproxy streams a body past
    # `stream_large_bodies` upstream before the `request` hook could look at it. The read goes
    # through; the body never does, whatever its size — nor trailers after it (header fields
    # sent once the body is done, which mitmproxy forwards after the `request` hook).
    import h2.config
    import h2.connection
    import h2.events

    proxy, recorder = scoped
    secret = b"EXFILTRATED-" * 4
    for size in (1, 200_000):  # buffered, and past `stream_large_bodies=1m` (streamed)
        ctx = ssl.create_default_context(cafile=str(proxy.ca))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_alpn_protocols(["h2"])
        with socket.create_connection((HOST, proxy.pport), timeout=10) as raw:
            raw.sendall(f"CONNECT {SCOPED}:443 HTTP/1.1\r\nHost: {SCOPED}:443\r\n\r\n".encode())
            assert raw.recv(4096).startswith(b"HTTP/1.1 200")
            with ctx.wrap_socket(raw, server_hostname=SCOPED) as tls:
                assert tls.selected_alpn_protocol() == "h2"
                conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
                conn.initiate_connection()
                conn.increment_flow_control_window(2**30)
                conn.send_headers(
                    1,
                    [
                        (":method", "GET"),
                        (":path", f"{PREFIX}chrome.zip"),
                        (":scheme", "https"),
                        (":authority", SCOPED),
                    ],
                )
                tls.sendall(conn.data_to_send())
                body, status, done = secret * size, None, False
                tls.settimeout(20)
                while not done:
                    window = min(conn.local_flow_control_window(1), conn.max_outbound_frame_size)
                    if body and window > 0:
                        conn.send_data(1, body[:window])
                        body = body[window:]
                        if not body:
                            conn.send_headers(1, [("x-trailer", "EXFILTRATED")], end_stream=True)
                        tls.sendall(conn.data_to_send())
                        continue
                    data = tls.recv(65535)
                    if not data:
                        break
                    for event in conn.receive_data(data):
                        if isinstance(event, h2.events.ResponseReceived):
                            status = dict(event.headers)[b":status"]
                        elif isinstance(event, (h2.events.StreamEnded, h2.events.StreamReset)):
                            done = True
                    tls.sendall(conn.data_to_send())
        assert status == b"200", (size, proxy.mitm_log.read_text())
    _settle()
    seen = recorder.received + recorder.after
    assert f"GET {PREFIX}chrome.zip".encode() in recorder.received, seen
    assert not any(b"EXFILTRATED" in r for r in seen), seen
