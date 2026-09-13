"""tests for the in-guest gVisor socket filter (`assets/sandbox/socket_filter.py`).

Two layers: the pure `strip_create`/`is_create` rewrite rules, and a real end-to-end run over
unix sockets — a fake upstream engine records what it receives while a client drives the filter,
so keep-alive framing, the fail-closed refusal and the hijack splice are exercised for real. The
module is a packaged in-guest asset (run by `python3` in the VM), so it's loaded by path."""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
import time
from pathlib import Path

import pytest

_ASSET = Path(__file__).resolve().parents[1] / "src/foldyard/assets/sandbox/socket_filter.py"
_spec = importlib.util.spec_from_file_location("fy_socket_filter", _ASSET)
assert _spec and _spec.loader
sf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sf)


# ── the rewrite rules ─────────────────────────────────────────────────────────────────


def test_strip_create_libpod_drops_oci_runtime_and_gvisor_annotations():
    body = json.dumps(
        {
            "name": "sib",
            "image": "img",
            "oci_runtime": "crun",
            "annotations": {"dev.gvisor.flag.host-uds": "all", "keep.me": "yes"},
        }
    ).encode()
    out = json.loads(sf.strip_create("/v5.0.0/libpod/containers/create", body))
    assert "oci_runtime" not in out
    assert out["annotations"] == {"keep.me": "yes"}  # only the gVisor namespace is stripped
    assert out["name"] == "sib" and out["image"] == "img"  # everything else survives


def test_strip_create_compat_drops_hostconfig_runtime():
    body = json.dumps(
        {"Image": "img", "HostConfig": {"Runtime": "crun", "Privileged": True}}
    ).encode()
    out = json.loads(sf.strip_create("/v1.41/containers/create", body))
    assert "Runtime" not in out["HostConfig"]
    assert out["HostConfig"]["Privileged"] is True  # the rest of HostConfig is untouched
    assert out["Image"] == "img"


def test_strip_create_no_runtime_field_is_a_noop():
    body = json.dumps({"name": "sib", "image": "img"}).encode()
    assert json.loads(sf.strip_create("/libpod/containers/create", body)) == {
        "name": "sib",
        "image": "img",
    }


def test_strip_create_refuses_unparseable_body():
    with pytest.raises(ValueError):
        sf.strip_create("/libpod/containers/create", b"not json {{{")


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/v5.0.0/libpod/containers/create", True),
        ("POST", "/v1.41/containers/create?name=x", True),
        ("POST", "/containers/create", True),
        ("GET", "/v5.0.0/libpod/info", False),
        ("POST", "/libpod/containers/prune", False),
        ("POST", "/libpod/images/create", False),  # image pull, not container create
    ],
)
def test_is_create(method, path, expected):
    assert sf.is_create(method, path) is expected


# ── end to end over unix sockets ────────────────────────────────────────────────────────


class FakeUpstream:
    """A minimal engine: records each request it receives (method, path, body) and replies with
    a canned response. Loops on one connection so keep-alive can be exercised."""

    def __init__(self, path: str, response: bytes):
        self.path = path
        self.response = response
        self.requests: list[dict] = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(path)
        self._srv.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        r = sf._Reader(conn)
        try:
            while True:
                line = r.read_line()
                if line is None:
                    return
                headers = r.read_headers()
                parts = line.split(b" ")
                cl = sf.header_value(headers, "content-length")
                te = sf.header_value(headers, "transfer-encoding")
                if te and b"chunked" in te.lower():
                    body = sf._read_chunked_whole(r)
                else:
                    body = r.read_exact(int(cl)) if cl else b""
                self.requests.append(
                    {"method": parts[0].decode(), "path": parts[1].decode(), "body": body}
                )
                conn.sendall(self.response)
        except (OSError, EOFError):
            return


def _wait_connectable(path: str, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(path)
            s.close()
            return
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"{path} never became connectable")


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A fake upstream + the filter in front of it; returns (upstream, connect()).

    Sockets are bound by short RELATIVE names from inside tmp_path — macOS caps an AF_UNIX path
    at ~104 bytes, which pytest's absolute tmp paths blow past."""
    monkeypatch.chdir(tmp_path)

    def build(response: bytes = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"):
        up = FakeUpstream("up.sock", response)
        listen = "filtered.sock"
        threading.Thread(target=sf.serve, args=(up.path, listen), daemon=True).start()
        _wait_connectable(listen)

        def connect():
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(listen)
            return c

        return up, connect

    return build


def _read_response(conn: socket.socket) -> tuple[bytes, list[bytes], bytes]:
    r = sf._Reader(conn)
    line = r.read_line()
    headers = r.read_headers()
    cl = sf.header_value(headers, "content-length")
    body = r.read_exact(int(cl)) if cl else b""
    return line, headers, body


def test_libpod_create_reaches_upstream_with_the_runtime_stripped(wired):
    up, connect = wired()
    body = json.dumps({"image": "img", "oci_runtime": "crun"}).encode()
    c = connect()
    c.sendall(
        b"POST /v5.0.0/libpod/containers/create HTTP/1.1\r\nHost: d\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    line, _, resp_body = _read_response(c)
    assert b"200" in line and resp_body == b"ok"
    assert len(up.requests) == 1
    sent = json.loads(up.requests[0]["body"])
    assert "oci_runtime" not in sent and sent["image"] == "img"


def test_get_is_forwarded_verbatim(wired):
    up, connect = wired(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\ninfo")
    c = connect()
    c.sendall(b"GET /v5.0.0/libpod/info HTTP/1.1\r\nHost: d\r\n\r\n")
    line, _, body = _read_response(c)
    assert b"200" in line and body == b"info"
    assert up.requests[0]["method"] == "GET" and up.requests[0]["path"].endswith("/info")


def test_keep_alive_filters_a_create_that_is_the_second_request(wired):
    """The bypass a naive proxy opens: a create pipelined after an innocuous request on the same
    reused connection must still be filtered."""
    up, connect = wired()
    body = json.dumps({"image": "img", "oci_runtime": "crun"}).encode()
    c = connect()
    c.sendall(b"GET /v5.0.0/libpod/info HTTP/1.1\r\nHost: d\r\n\r\n")
    _read_response(c)
    c.sendall(
        b"POST /v5.0.0/libpod/containers/create HTTP/1.1\r\nHost: d\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    _read_response(c)
    assert len(up.requests) == 2
    assert "oci_runtime" not in json.loads(up.requests[1]["body"])


def test_unparseable_create_is_refused_and_never_forwarded(wired):
    up, connect = wired()
    junk = b"not json {{{"
    c = connect()
    c.sendall(
        b"POST /v5.0.0/libpod/containers/create HTTP/1.1\r\nHost: d\r\n"
        b"Content-Length: " + str(len(junk)).encode() + b"\r\n\r\n" + junk
    )
    line, _, _ = _read_response(c)
    assert b"400" in line
    assert up.requests == []  # fail closed: the engine never saw it


def test_chunked_create_body_is_dechunked_and_stripped(wired):
    up, connect = wired()
    body = json.dumps({"image": "img", "oci_runtime": "crun"}).encode()
    chunked = b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)
    c = connect()
    c.sendall(
        b"POST /v5.0.0/libpod/containers/create HTTP/1.1\r\nHost: d\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n" + chunked
    )
    _read_response(c)
    assert len(up.requests) == 1
    assert "oci_runtime" not in json.loads(up.requests[0]["body"])


def test_upgrade_connection_is_spliced_raw_both_ways(tmp_path, monkeypatch):
    """A hijacked connection (101 upgrade) becomes a raw byte pipe: the filter stops parsing and
    copies both directions — attach/exec streams."""
    monkeypatch.chdir(tmp_path)
    up_path = "up.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(up_path)
    srv.listen(8)

    def serve_conn(conn):
        r = sf._Reader(conn)
        try:
            if r.read_line() is None:  # a bare connectivity probe — no request
                return
            r.read_headers()
        except EOFError:
            return
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: tcp\r\n\r\n")
        conn.sendall(b"from-engine")
        conn.sendall(b"echo:" + conn.recv(64))  # echo whatever streams in after the upgrade

    def upstream():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=serve_conn, args=(conn,), daemon=True).start()

    threading.Thread(target=upstream, daemon=True).start()
    listen = "filtered.sock"
    threading.Thread(target=sf.serve, args=(up_path, listen), daemon=True).start()
    _wait_connectable(listen)

    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(listen)
    c.sendall(b"POST /libpod/exec/x/start HTTP/1.1\r\nUpgrade: tcp\r\nConnection: Upgrade\r\n\r\n")
    time.sleep(0.1)
    c.sendall(b"stdin!")
    c.settimeout(3)
    seen = b""
    while b"echo:stdin!" not in seen:
        chunk = c.recv(64)
        if not chunk:
            break
        seen += chunk
    assert b"101 Switching Protocols" in seen
    assert b"from-engine" in seen and b"echo:stdin!" in seen
