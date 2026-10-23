"""Guest-side client: W workers, each doing N new connections (connect, tiny GET, read, close).

client.py <host> <ports,comma> <N> <W> [--rate R] [--timeout S] [--out file.jsonl] [--hold]
  ports   : each worker w uses ports[w % len(ports)] (one vs many destinations)
  --rate  : cap each worker at R connections/s (0 = as fast as possible)
  --timeout: connect timeout (default 130 s, longer than the kernel's own SYN give-up)
  --retry S: on connect timeout S, close and re-dial (a short-timeout-and-retry client)
  --out   : one JSON line per connection (t0, connect seconds, local port, worker)
Prints a summary line.
"""

import argparse
import json
import socket
import struct
import threading
import time

ap = argparse.ArgumentParser()
ap.add_argument("host")
ap.add_argument("ports")
ap.add_argument("n", type=int)
ap.add_argument("w", type=int)
ap.add_argument("--rate", type=float, default=0)
ap.add_argument("--timeout", type=float, default=130)
ap.add_argument("--retry", type=float, default=0)
ap.add_argument("--out")
ap.add_argument("--nodata", action="store_true", help="connect and close, no request")
ap.add_argument(
    "--abort",
    action="store_true",
    help="after reading the peer's EOF, close with SO_LINGER 0 (RST, not FIN)",
)
a = ap.parse_args()
ports = [int(p) for p in a.ports.split(",")]
out = open(a.out, "a", buffering=1) if a.out else None
lock = threading.Lock()
durs, errs, slow, retries = [], [], [], [0]


def dial(addr):
    if not a.retry:
        return socket.create_connection(addr, timeout=a.timeout)
    deadline = time.monotonic() + a.timeout
    while True:
        try:
            return socket.create_connection(addr, timeout=a.retry)
        except TimeoutError:
            with lock:
                retries[0] += 1
            if time.monotonic() > deadline:
                raise


def worker(w):
    port = ports[w % len(ports)]
    gap = 1.0 / a.rate if a.rate else 0
    nxt = time.monotonic()
    for _ in range(a.n):
        if gap:
            nxt += gap
        t0 = time.time()
        p0 = time.perf_counter()
        lp = None
        try:
            s = dial((a.host, port))
            d = time.perf_counter() - p0
            lp = s.getsockname()[1]
            s.settimeout(30)
            if not a.nodata:
                s.sendall(b"GET /small HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                while s.recv(65536):
                    pass
            if a.abort:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.close()
            err = None
        except Exception as e:
            d = time.perf_counter() - p0
            err = repr(e)[:80]
        with lock:
            durs.append(d)
            if err:
                errs.append(err)
            if d > 1:
                slow.append((round(t0 - T0, 2), round(d, 2), w))
            if out:
                out.write(
                    json.dumps(
                        {
                            "t0": round(t0, 4),
                            "c": round(d, 5),
                            "lp": lp,
                            "w": w,
                            "port": port,
                            "err": err,
                        }
                    )
                    + "\n"
                )
        if gap:
            time.sleep(max(0, nxt - time.monotonic()))


T0 = time.time()
th = [threading.Thread(target=worker, args=(w,)) for w in range(a.w)]
for x in th:
    x.start()
for x in th:
    x.join()
wall = time.time() - T0
durs.sort()
q = lambda p: round(durs[min(len(durs) - 1, int(p * len(durs)))] * 1000, 2)  # noqa: E731
print(
    json.dumps(
        {
            "start": round(T0, 2),
            "n": len(durs),
            "w": a.w,
            "ports": ports,
            "rate": a.rate,
            "wall_s": round(wall, 1),
            "conn_per_s": round(len(durs) / wall, 1),
            "p50_ms": q(0.5),
            "p99_ms": q(0.99),
            "max_ms": q(1),
            "slow_gt1s": len(slow),
            "slow": slow[:40],
            "errors": len(errs),
            "err_sample": errs[:3],
            "retries": retries[0],
        }
    )
)
