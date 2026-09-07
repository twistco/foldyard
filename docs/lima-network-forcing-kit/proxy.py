#!/usr/bin/env python3
"""Explicit forcing proxy (HTTP CONNECT + absolute-URI HTTP) for the Lima forcing proof.

The nftables wall makes THIS proxy the only reachable egress for the `agent` user; the agent
is handed HTTP(S)_PROXY pointing here. Per request we read the target host and apply MODE,
read PER REQUEST from /etc/wall/mode (so it toggles at runtime, no restart):

  filtered     ALLOW allowlisted hosts, BLOCK (403) the rest
  passthrough  ALLOW everything, log only

No TLS termination (HTTPS is a CONNECT tunnel), so no CA needed — this proves *forcing* and
*filtering*. A client that ignores the proxy env reaches nothing (the wall drops direct egress):
fail-closed forcing, which is the whole point.
"""

import select
import socket
import sys
import threading
import time

PORT = 8080
ALLOWFILE = "/etc/wall/allowlist"
MODEFILE = "/etc/wall/mode"
LOGFILE = "/var/log/transproxy.log"


def read_mode():
    try:
        with open(MODEFILE) as f:
            m = f.read().strip().lower()
            return m if m in ("filtered", "passthrough") else "filtered"
    except OSError:
        return "filtered"


def load_allow():
    try:
        with open(ALLOWFILE) as f:
            return {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}
    except OSError:
        return set()


def log(msg):
    line = f"{int(time.time())} {msg}\n"
    try:
        with open(LOGFILE, "a") as f:
            f.write(line)
    except OSError:
        pass
    sys.stderr.write(line)
    sys.stderr.flush()


def allowed(host):
    if read_mode() == "passthrough":
        return True
    allow = load_allow()
    return bool(host) and any(host == a or host.endswith("." + a) for a in allow)


def pipe(src, dst):
    """Relay ONE direction: read src, write dst. handle() runs the two directions in two threads,
    so each socket is recv()'d by exactly one thread. (A full-duplex pipe that select()s on BOTH
    ends, run twice with swapped args, has both threads recv() the same socket — they split the
    byte stream between them and interleave the writes, shredding it. TLS then fails with
    'bad record MAC' on any transfer spanning more than one recv — e.g. a multi-MB image blob,
    while a tiny curl response slips through the narrow race. That's the bug this avoids.)"""
    try:
        while True:
            r, _, _ = select.select([src], [], [], 120)
            if not r:
                return
            data = src.recv(65536)
            if not data:
                return
            dst.sendall(data)
    except Exception:
        return


def read_headers(conn):
    conn.settimeout(10)
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 65536:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def host_port_from_http(req, target):
    host, port = None, 80
    if "://" in target:  # absolute-form: GET http://host:port/path
        hostport = target.split("://", 1)[1].split("/", 1)[0]
        host = hostport.split(":")[0]
        if ":" in hostport:
            port = int(hostport.split(":")[1])
    if not host:  # fall back to the Host header
        for line in req.split(b"\r\n"):
            if line[:5].lower() == b"host:":
                hp = line[5:].strip().decode("latin1", "ignore")
                host = hp.split(":")[0]
                if ":" in hp:
                    port = int(hp.split(":")[1])
                break
    return host, port


def handle(conn):
    up = None
    try:
        req = read_headers(conn)
        if not req:
            return
        parts = req.split(b"\r\n", 1)[0].decode("latin1", "ignore").split()
        if len(parts) < 2:
            return
        method, target = parts[0].upper(), parts[1]

        if method == "CONNECT":  # HTTPS tunnel: CONNECT host:port
            host = target.split(":")[0]
            port = int(target.split(":")[1]) if ":" in target else 443
        else:  # plain HTTP via proxy (absolute URI)
            host, port = host_port_from_http(req, target)

        mode = read_mode()
        if not allowed(host):
            log(f"BLOCK {host}:{port} mode={mode}")
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        log(f"ALLOW {host}:{port} mode={mode}")
        up = socket.create_connection((host, port), timeout=10)
        if method == "CONNECT":
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:
            up.sendall(req)  # forward the original request bytes

        conn.settimeout(None)
        t = threading.Thread(target=pipe, args=(conn, up), daemon=True)
        t.start()
        pipe(up, conn)
    except Exception as e:
        log(f"ERR {e}")
    finally:
        for s in (conn, up):
            try:
                if s:
                    s.close()
            except Exception:
                pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(128)
    log(f"forcing proxy up on 127.0.0.1:{PORT} mode={read_mode()} allow={sorted(load_allow())}")
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == "__main__":
    main()
