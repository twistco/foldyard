"""Host-side origin for the usernet-freeze spike. Loopback only.

origin.py <log.jsonl> <port>[:mode] ...
  mode http  (default): read a request head, answer 100 bytes, close (the server closes first)
  mode echo           : echo every byte back (for an established-connection canary)
  mode hang           : listen(1) and never accept, so a dial to it hangs once the queue is full
  mode slowaccept=S   : accept, then sleep S seconds before answering (holds the hostagent's
                        relay goroutine but not its dial)
Each accept is logged as one JSON line: {"t": epoch, "port": local, "peer": peer port}.
"""

import asyncio
import json
import socket
import sys
import time

LOG = open(sys.argv[1], "a", buffering=1)


def log(**kw):
    kw["t"] = round(time.time(), 4)
    LOG.write(json.dumps(kw) + "\n")


def make_http(port, delay=0.0):
    async def handle(reader, writer):
        peer = writer.get_extra_info("peername")[1]
        log(port=port, peer=peer, ev="accept")
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                b = await reader.read(65536)
                if not b:
                    break
                buf += b
            if delay:
                await asyncio.sleep(delay)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n" + b"x" * 100
            )
            await writer.drain()
        except Exception as e:
            log(port=port, peer=peer, ev="err", err=repr(e)[:80])
        finally:
            writer.close()

    return handle


def make_echo(port):
    async def handle(reader, writer):
        peer = writer.get_extra_info("peername")[1]
        log(port=port, peer=peer, ev="accept")
        while True:
            b = await reader.read(65536)
            if not b:
                break
            writer.write(b)
            await writer.drain()
        writer.close()

    return handle


async def main():
    hang_socks = []
    for spec in sys.argv[2:]:
        port, _, mode = spec.partition(":")
        port = int(port)
        mode = mode or "http"
        if mode == "hang":
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            s.listen(1)
            hang_socks.append(s)
            continue
        if mode == "echo":
            h = make_echo(port)
        elif mode.startswith("slowaccept="):
            h = make_http(port, float(mode.split("=")[1]))
        else:
            h = make_http(port)
        await asyncio.start_server(h, "127.0.0.1", port, backlog=1024, reuse_address=True)
    log(ev="ready", ports=sys.argv[2:])
    await asyncio.Event().wait()


asyncio.run(main())
