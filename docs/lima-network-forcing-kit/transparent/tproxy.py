#!/usr/bin/env python3
"""Transparent forcing proxy (SO_ORIGINAL_DST + SNI/Host peek) for the container-egress wall.

Container traffic to :80/:443 is REDIRECTed here by nftables (in PREROUTING — the reliable
path). We recover the ORIGINAL destination via SO_ORIGINAL_DST, peek the hostname (TLS SNI for
:443, the Host header for :80), then apply MODE, read PER REQUEST from /etc/wall/mode:

  filtered     ALLOW allowlisted hosts (splice), BLOCK the rest
  passthrough  ALLOW everything (splice), log only

No TLS termination (we splice the real bytes), so no CA needed. This is *transparent*: the
container needs no proxy env, so non-cooperative clients (Go, gRPC, raw sockets) are caught too.
"""

import select
import socket
import struct
import sys
import threading
import time

SO_ORIGINAL_DST = 80
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


def original_dst(conn):
    raw = conn.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
    port = struct.unpack(">H", raw[2:4])[0]
    ip = socket.inet_ntoa(raw[4:8])
    return ip, port


def sni_from_clienthello(b):
    try:
        if not b or b[0] != 0x16:  # TLS handshake record
            return None
        hs = b[5:]  # skip 5-byte record header
        if not hs or hs[0] != 0x01:  # ClientHello
            return None
        p = 4 + 2 + 32  # handshake hdr(4) + version(2) + random(32)
        p += 1 + hs[p]  # session id
        p += 2 + struct.unpack(">H", hs[p : p + 2])[0]  # cipher suites
        p += 1 + hs[p]  # compression methods
        p += 2  # extensions length
        n = len(hs)
        while p + 4 <= n:
            et = struct.unpack(">H", hs[p : p + 2])[0]
            el = struct.unpack(">H", hs[p + 2 : p + 4])[0]
            p += 4
            if et == 0x00:  # server_name extension
                nlen = struct.unpack(">H", hs[p + 3 : p + 5])[0]
                return hs[p + 5 : p + 5 + nlen].decode("ascii", "ignore")
            p += el
    except Exception:
        return None
    return None


def host_from_http(b):
    try:
        for line in b.split(b"\r\n"):
            if line[:5].lower() == b"host:":
                return line[5:].strip().split(b":")[0].decode("ascii", "ignore")
    except Exception:
        pass
    return None


def host_allowed(host):
    if read_mode() == "passthrough":
        return True
    allow = load_allow()
    return bool(host) and any(host == a or host.endswith("." + a) for a in allow)


def pipe(src, dst):
    """Relay ONE direction: read src, write dst. handle() runs the two directions in two threads,
    so each socket is recv()'d by exactly one thread. (A full-duplex pipe that select()s on BOTH
    ends, run twice with swapped args, has both threads recv() the same socket — they split the
    byte stream and interleave the writes, shredding it. TLS then fails with 'bad record MAC' on
    any transfer spanning more than one recv — e.g. a multi-MB image blob, while a tiny curl
    response slips through the narrow race. That's the bug this avoids.)"""
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


def handle(conn):
    up = None
    try:
        ip, port = original_dst(conn)
        conn.settimeout(8)
        head = conn.recv(8192)  # consume the first segment (ClientHello / request line)
        host = (sni_from_clienthello(head) if port == 443 else host_from_http(head)) or ip
        mode = read_mode()
        if not host_allowed(host):
            log(f"BLOCK {host} ({ip}:{port}) mode={mode}")
            return
        log(f"ALLOW {host} ({ip}:{port}) mode={mode}")
        up = socket.create_connection((ip, port), timeout=8)
        conn.settimeout(None)
        if head:
            up.sendall(head)
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
    srv.bind(("0.0.0.0", PORT))
    srv.listen(128)
    log(f"transparent proxy up on :{PORT} mode={read_mode()} allow={sorted(load_allow())}")
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == "__main__":
    main()
