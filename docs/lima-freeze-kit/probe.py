"""Every second for S seconds, one fresh connect (timeout 3 s) to each target; one line per
round: elapsed, then per target the connect ms or 'X' for a timeout/error.
probe.py <S> <host:port> ..."""

import socket
import sys
import time

S = float(sys.argv[1])
tg = [(a.rsplit(":", 1)[0], int(a.rsplit(":", 1)[1])) for a in sys.argv[2:]]
T0 = time.time()
while time.time() - T0 < S:
    row = []
    for h, p in tg:
        t = time.perf_counter()
        try:
            c = socket.create_connection((h, p), timeout=3)
            c.close()
            row.append(f"{(time.perf_counter() - t) * 1000:.0f}")
        except Exception:
            row.append("X")
    print(
        f"{time.time():.1f} +{time.time() - T0:5.1f}s "
        + " ".join(f"{h}:{p}={r}" for (h, p), r in zip(tg, row, strict=True)),
        flush=True,
    )
    time.sleep(max(0, 1 - (time.time() - T0) % 1))
