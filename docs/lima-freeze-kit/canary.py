"""Established-connection canary: one long-lived TCP connection to an echo port; send 16 bytes
every 50 ms and log any round trip over 200 ms. canary.py <host> <port> <seconds>"""

import socket
import sys
import time

s = socket.create_connection((sys.argv[1], int(sys.argv[2])))
end = time.time() + float(sys.argv[3])
n = 0
worst = 0
slow = []
while time.time() < end:
    t = time.perf_counter()
    s.sendall(b"x" * 16)
    got = 0
    while got < 16:
        got += len(s.recv(16 - got))
    d = time.perf_counter() - t
    n += 1
    worst = max(worst, d)
    if d > 0.2:
        slow.append((round(time.time(), 2), round(d, 3)))
    time.sleep(0.05)
print({"canary_rtts": n, "worst_ms": round(worst * 1000, 2), "over_200ms": slow})
