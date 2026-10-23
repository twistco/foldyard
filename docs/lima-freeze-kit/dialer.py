"""During a stall: dial a list of ports once a second for S seconds, log connect time per port.
dialer.py <host> <ports,comma> <seconds>"""

import socket
import sys
import time

ports = [int(p) for p in sys.argv[2].split(",")]
end = time.time() + float(sys.argv[3])
worst = {}
while time.time() < end:
    for p in ports:
        t = time.perf_counter()
        try:
            c = socket.create_connection((sys.argv[1], p), timeout=5)
            c.close()
            d = time.perf_counter() - t
        except Exception:
            d = 99
        worst[p] = max(worst.get(p, 0), d)
    time.sleep(1)
print({"dialer_worst_ms": {p: round(v * 1000, 1) for p, v in worst.items()}})
