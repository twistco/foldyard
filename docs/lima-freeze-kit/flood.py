"""N threads, each ONE connect to host:port (timeout T), then a tiny GET. Prints a summary of
connect times. flood.py <host> <port> <N> <T>"""

import collections
import json
import socket
import sys
import threading
import time

H, P, N, T = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
res = []
lock = threading.Lock()
T0 = time.time()


def one():
    t = time.perf_counter()
    try:
        s = socket.create_connection((H, P), timeout=T)
        d = time.perf_counter() - t
        r = "ok"
        s.settimeout(T)
        s.sendall(b"GET /small HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        while s.recv(65536):
            pass
        s.close()
    except Exception as e:
        d = time.perf_counter() - t
        r = type(e).__name__
    with lock:
        res.append((round(d, 1), r))


th = [threading.Thread(target=one) for _ in range(N)]
[x.start() for x in th]
[x.join() for x in th]
print(
    json.dumps(
        {
            "flood_start": round(T0, 1),
            "n": N,
            "by_result": dict(collections.Counter(r for _, r in res)),
            "connect_s_hist": dict(
                sorted(collections.Counter(int(d // 5) * 5 for d, _ in res).items())
            ),
        }
    )
)
