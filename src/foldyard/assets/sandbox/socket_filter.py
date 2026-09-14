#!/usr/bin/env python3
"""foldyard gVisor socket filter — runs in the machine VM, in front of the runsc-default podman
API socket, so the dev box's own engine socket cannot opt a container back out of gVisor.

The box mounts THIS socket (``podman-runsc-filtered.sock``) as ``/var/run/docker.sock``. Every
request is forwarded to the upstream runsc-default socket unchanged EXCEPT container-create,
whose body is rewritten to drop the fields that would pick a different runtime:

  - libpod  (``POST .../libpod/containers/create``): drop top-level ``oci_runtime`` and any
    ``annotations`` key under ``dev.gvisor.`` (the runsc flag namespace).
  - compat  (``POST .../containers/create``):        drop ``HostConfig.Runtime``.

With the field gone the create falls to the socket's own default, which is ``runsc-fy``. On
podman < 6 the field was ignored anyway; this is the enforcement tier for podman >= 6, where the
libpod create endpoint honours a client-supplied runtime (docs/isolation-layers.md, ADR ③).

Fail closed: a create whose body cannot be parsed as JSON is REFUSED (400), never forwarded — an
unparseable body could smuggle a runtime past the strip. Stdlib only, one thread per connection.
Keep-alive is preserved by framing each response (Content-Length / chunked / read-until-close),
so a create is filtered even when it is the second request on a reused connection — the bypass a
naive "inspect the first request then splice" proxy would open. Once a connection is hijacked for
streaming (a 101 upgrade, or a response with no length framing — attach/exec/logs), the filter
stops parsing and splices raw bytes; no discrete create is ever pipelined after a hijack, so
nothing escapes the filter that way either.
"""

from __future__ import annotations

import json
import os
import posixpath
import socket
import sys
import threading
from urllib.parse import unquote

_CREATE_SUFFIX = "/containers/create"
_GVISOR_ANNOTATION_PREFIX = "dev.gvisor."
_CHUNK = 65536


class _Reader:
    """Buffered reader over a socket: exact-N and CRLF-line reads, with the leftover buffer
    exposed so a hijack handover can flush what was already pulled off the wire."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""

    def _fill(self) -> bool:
        chunk = self.sock.recv(_CHUNK)
        if not chunk:
            return False
        self.buf += chunk
        return True

    def read_line(self) -> bytes | None:
        """A line without its trailing CRLF; ``None`` at a clean EOF (nothing buffered)."""
        while b"\r\n" not in self.buf:
            if not self._fill():
                if self.buf:
                    raise EOFError("connection closed mid-line")
                return None
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            if not self._fill():
                raise EOFError("connection closed mid-body")
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_headers(self) -> list[bytes]:
        """Header lines up to (and consuming) the blank separator."""
        lines: list[bytes] = []
        while True:
            line = self.read_line()
            if line is None:
                raise EOFError("connection closed in headers")
            if line == b"":
                return lines
            lines.append(line)


def header_value(lines: list[bytes], name: str) -> bytes | None:
    """The value of header ``name`` (case-insensitive), or ``None``."""
    want = name.lower().encode()
    for ln in lines:
        if b":" in ln:
            k, v = ln.split(b":", 1)
            if k.strip().lower() == want:
                return v.strip()
    return None


def route_path(path: str) -> str:
    """The request path as the engine's router sees it: query dropped, percent-decoded and
    normalised (``//``, ``.`` and ``..`` collapsed). Matching the RAW target would let
    ``/containers/%63reate`` or ``/containers/./create`` reach the engine as a create the filter
    never inspected; the decoded form is what both create checks judge."""
    return posixpath.normpath(unquote(path.split("?", 1)[0]))


def is_create(method: str, path: str) -> bool:
    return method == "POST" and route_path(path).endswith(_CREATE_SUFFIX)


def strip_create(path: str, body: bytes) -> bytes:
    """Rewrite a container-create body, removing the runtime-selecting fields. Raises
    ``ValueError`` if the body is not a JSON object — the caller refuses it rather than forward
    a create it could not inspect."""
    data = json.loads(body or b"{}")
    if not isinstance(data, dict):
        raise ValueError("create body is not a JSON object")
    if "/libpod/" in route_path(path):
        data.pop("oci_runtime", None)
        annotations = data.get("annotations")
        if isinstance(annotations, dict):
            for key in [k for k in annotations if k.startswith(_GVISOR_ANNOTATION_PREFIX)]:
                annotations.pop(key, None)
    else:  # docker-compat create — the runtime lives under HostConfig
        host_config = data.get("HostConfig")
        if isinstance(host_config, dict):
            host_config.pop("Runtime", None)
    return json.dumps(data).encode()


def _head(start_line: bytes, headers: list[bytes]) -> bytes:
    """A start line + header block, terminated — correct for an EMPTY header list too (a bare
    ``100 Continue``), where joining on CRLF would emit a stray blank line."""
    return start_line + b"\r\n" + b"".join(ln + b"\r\n" for ln in headers) + b"\r\n"


def _rebuild_headers(request_line: bytes, headers: list[bytes], body_len: int) -> bytes:
    """The request line + headers for a rewritten create: force Content-Length to the new body
    and drop any chunked framing (the body is sent whole)."""
    kept = [
        ln
        for ln in headers
        if ln.split(b":", 1)[0].strip().lower()
        not in (b"content-length", b"transfer-encoding", b"expect")
    ]
    kept.append(b"Content-Length: " + str(body_len).encode())
    return _head(request_line, kept)


def _forward_body(src: _Reader, dst: socket.socket, te: bytes | None, cl: bytes | None) -> str:
    """Copy a body from ``src`` to ``dst`` PRESERVING its framing. Returns ``"framed"`` when a
    body was copied, or ``"none"`` when there was no length framing (the caller decides: a
    request has no body, a response must be spliced as a raw stream)."""
    if te and b"chunked" in te.lower():
        while True:
            size_line = src.read_line()
            if size_line is None:
                return "framed"
            dst.sendall(size_line + b"\r\n")
            size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                while True:  # trailers, up to the blank line
                    trailer = src.read_line()
                    dst.sendall((trailer or b"") + b"\r\n")
                    if not trailer:
                        return "framed"
            dst.sendall(src.read_exact(size + 2))  # chunk data + its CRLF
    if cl is not None:
        remaining = int(cl)
        while remaining > 0:
            data = src.read_exact(min(remaining, _CHUNK))
            dst.sendall(data)
            remaining -= len(data)
        return "framed"
    return "none"


def _splice(client: socket.socket, upstream: socket.socket, cr: _Reader, ur: _Reader) -> None:
    """Bidirectional raw copy until either side closes — for a hijacked/streamed connection.
    Buffered-but-unsent bytes on each reader are flushed first."""
    if cr.buf:
        upstream.sendall(cr.buf)
        cr.buf = b""
    if ur.buf:
        client.sendall(ur.buf)
        ur.buf = b""

    def pump(a: socket.socket, b: socket.socket) -> None:
        try:
            while True:
                data = a.recv(_CHUNK)
                if not data:
                    break
                b.sendall(data)
        except OSError:
            pass
        finally:
            try:
                b.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threads = [
        threading.Thread(target=pump, args=(client, upstream), daemon=True),
        threading.Thread(target=pump, args=(upstream, client), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def _send_400(client: socket.socket, message: str) -> None:
    body = message.encode()
    client.sendall(
        b"HTTP/1.1 400 Bad Request\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )


def handle_conn(client: socket.socket, upstream_path: str) -> None:
    """Proxy one client connection to a fresh upstream connection, filtering creates and
    preserving keep-alive until the connection ends or is hijacked."""
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        upstream.connect(upstream_path)
    except OSError:
        _send_400(client, "foldyard socket filter: upstream engine socket unreachable")
        client.close()
        return
    cr, ur = _Reader(client), _Reader(upstream)
    try:
        while True:
            request_line = cr.read_line()
            if request_line is None:
                return
            headers = cr.read_headers()
            parts = request_line.split(b" ")
            method = parts[0].decode(errors="replace") if parts else ""
            path = parts[1].decode(errors="replace") if len(parts) > 1 else ""
            te = header_value(headers, "transfer-encoding")
            cl = header_value(headers, "content-length")
            upgrade = header_value(headers, "upgrade") is not None

            if is_create(method, path):
                # The body is read whole before anything reaches upstream, so a client waiting
                # on `Expect: 100-continue` would stall: answer the interim ourselves (the
                # header is dropped from the rebuilt request — the body goes up in one piece).
                expect = header_value(headers, "expect")
                if expect and b"100-continue" in expect.lower():
                    client.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
                if te and b"chunked" in te.lower():
                    body = _read_chunked_whole(cr)
                elif cl is not None:
                    body = cr.read_exact(int(cl))
                else:
                    body = b""
                try:
                    new_body = strip_create(path, body)
                except ValueError:
                    _send_400(client, "foldyard socket filter: unparseable create body refused")
                    return
                upstream.sendall(_rebuild_headers(request_line, headers, len(new_body)) + new_body)
            else:
                upstream.sendall(_head(request_line, headers))
                _forward_body(cr, upstream, te, cl)

            # Response. Interim 1xx responses (a 100 Continue the engine emits for a forwarded
            # Expect, 103 hints) carry no body and precede the final one: forward each and keep
            # reading, or the final response's framing is never applied and the connection is
            # spliced raw — after which a pipelined create would bypass the filter.
            while True:
                response_line = ur.read_line()
                if response_line is None:
                    return
                resp_headers = ur.read_headers()
                client.sendall(_head(response_line, resp_headers))
                status = _status_code(response_line)
                if not (100 <= status < 200) or status == 101:
                    break

            if status == 101 or upgrade:
                _splice(client, upstream, cr, ur)
                return
            if method == "HEAD" or status in (204, 304):
                pass  # never a body, regardless of a Content-Length header
            else:
                r_te = header_value(resp_headers, "transfer-encoding")
                r_cl = header_value(resp_headers, "content-length")
                if _forward_body(ur, client, r_te, r_cl) == "none":
                    # No length framing and not an upgrade: a raw/streamed response body.
                    _splice(client, upstream, cr, ur)
                    return
            if _is_close(headers) or _is_close(resp_headers):
                return
    except (EOFError, OSError):
        return
    finally:
        for s in (upstream, client):
            try:
                s.close()
            except OSError:
                pass


def _read_chunked_whole(src: _Reader) -> bytes:
    """De-chunk a whole request body into bytes (used only to rewrite a create)."""
    out = b""
    while True:
        size_line = src.read_line()
        if size_line is None:
            return out
        size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
        if size == 0:
            while True:
                trailer = src.read_line()
                if not trailer:
                    return out
        out += src.read_exact(size)
        src.read_exact(2)  # trailing CRLF


def _status_code(response_line: bytes) -> int:
    fields = response_line.split(b" ")
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return 0


def _is_close(headers: list[bytes]) -> bool:
    conn = header_value(headers, "connection")
    return conn is not None and conn.lower() == b"close"


def serve(upstream_path: str, listen_path: str) -> None:
    try:
        os.unlink(listen_path)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(listen_path)
    os.chmod(listen_path, 0o660)
    srv.listen(128)
    while True:
        client, _ = srv.accept()
        threading.Thread(target=handle_conn, args=(client, upstream_path), daemon=True).start()


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write("usage: fy-socket-filter <upstream.sock> <listen.sock>\n")
        return 2
    serve(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
