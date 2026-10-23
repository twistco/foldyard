"""fy-fwd — foldyard's attributing forwarder (ADR-0034). Runs IN THE GUEST as root, under systemd
(``fy-fwd.service``), from the copy the forwarder's boot script wrote: foldyard's own packaged
code, never anything from the repo mount. Stdlib only: it runs under the guest's
``/usr/bin/python3``.

The walled VM's containers reach the host-side daemons (the egress proxy, the token services) at
the host gateway. Every such connection is re-made twice on the way, so by the time it reaches the
proxy nothing says which container sent it. This forwarder sits at the last point where that is
still known and is made the only way through:

* **The bridge listener**, inside the VM user's rootless network namespace, where every bridge
  container has its own address. An nft table there redirects a container's connection to a
  gateway daemon port onto this listener (``SO_ORIGINAL_DST`` gives the port back), so the proxy
  URL containers already have keeps working unchanged. The namespace is torn down with the last
  bridge container and re-created with the next one, so the forwarder follows it
  (:class:`Follower`).
* **The VM listener**, in the VM's own namespace on a loopback address Lima's port forwarding
  ignores, for what never enters the bridge namespace: build steps, image pulls, ``--network host``
  containers, the VM user's own processes. A policy route sends the walled principals' (the VM user
  and its subuids) gateway-port connections out through ``lo`` and an nft rule redirects them here,
  where each is attributed by its owning socket's cgroup (:func:`classify`). A pasta helper is
  refused: its connections are some container's, and nothing here can say whose.

On each connection to a proxy port the forwarder reads the request head (plain text even for HTTPS:
``CONNECT host:443``), removes every ``X-Fy-Origin`` line the client wrote, appends its own — where
the connection came from, a per-boot sequence and an HMAC under a key only root can read here
(``/etc/fy-fwd/forwarder.key``, the host's) — and then splices bytes both ways, parsing nothing
more; a first head that isn't a canonical HTTP/1 request is refused, never passed on unstamped.
Connections to the other daemon ports (token services) are spliced untouched. Upstream
connections leave from the VM's own namespace as root, which the VM firewall already admits; its
own table refuses the walled principals' direct connections to those ports, so a dead forwarder
means a refused connection, never unattributed egress.

What it reports, world-readable, for the host and the OCI hook: ``/run/fy-fwd/state`` (one line)
and ``/run/fy-fwd/netns`` (the inode of the rootless namespace it serves, which the hook waits for
before a container starts).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time

HEADER = b"x-fy-origin"
STAMP_HEADER = "X-Fy-Origin"
MAX_HEAD = 64 * 1024
HEAD_TIMEOUT = 10.0
# The upstream connect, through Lima's user-mode network to the proxy on your computer: a healthy
# one takes ~0.2 ms, and one that hasn't answered in 2 s is a SYN gVisor dropped (it reused a tuple
# still in gVisor's TIME_WAIT, google/gvisor#15013), which a new socket — a new source port —
# gets past. Bounded, then the client's connection fails as any failed connect does
# (docs/lima-usernet-freeze.md, Finding 10: 2 s and re-dial bounded a connect at 10 s, not 68 s).
CONNECT_TIMEOUT = 2.0
CONNECT_ATTEMPTS = 8
# How long a close waits for the upstream to acknowledge every byte sent to it before it may
# reset the connection; past it, the close is a FIN (an RST discards unacknowledged bytes).
DRAIN_WAIT = 2.0
SIOCOUTQ = 0x5411  # Linux: bytes in a TCP socket's send queue, unsent or unacknowledged
CLONE_NEWNET = 0x40000000
SO_ORIGINAL_DST = 80
STATE_DIR = "/run/fy-fwd"
SCOPE = re.compile(r"^libpod-([0-9a-f]{64})\.scope$")
# A build step's cgroup: crun's scope when the podman CLI builds (`crun-buildah-buildah<N>.scope`),
# buildah's own cgroup when the engine API does (`buildah-buildah<N>`: the host's `fy up`, the box).
BUILD_SCOPE = re.compile(r"^(crun-)?buildah-buildah[0-9]+(\.scope)?$")
PASTA = {"pasta", "pasta.avx2", "passt", "passt.avx2", "slirp4netns"}
REQUEST_LINE = re.compile(rb"\A[A-Z]+ \S+ HTTP/1\.[01]\Z")
BARE_LF = re.compile(rb"(?<!\r)\n")
# The names nft and the routing table go by: one table per namespace, replaced atomically.
TABLE = "fy_fwd"
RULE_PRIORITY = 4747  # this and the next: the policy rules, deleted and re-added as a pair
ROUTE_TABLE = 4747


# ── pure parts (unit-tested on the host) ──────────────────────────────────────────────


def parse_ports(text: str) -> list[tuple[int, int]]:
    """``"41000-41089, 41100-41189"`` (the wall's nft spelling) → ``[(41000, 41089), …]``."""
    out = []
    for part in text.replace(",", " ").split():
        lo, _, hi = part.partition("-")
        a, b = int(lo), int(hi or lo)
        if not (0 < a <= b < 65536):
            raise ValueError(f"bad port range {part!r}")
        out.append((a, b))
    return out


def each_port(ranges: list[tuple[int, int]]) -> list[int]:
    return [p for a, b in ranges for p in range(a, b + 1)]


def in_ranges(port: int | None, ranges: list[tuple[int, int]]) -> bool:
    return port is not None and any(a <= port <= b for a, b in ranges)


def nft_ranges(ranges: list[tuple[int, int]]) -> str:
    return ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in ranges)


def vm_dest_ok(dest, local, ranges: list[tuple[int, int]], gw: str) -> bool:
    """A VM-listener connection is one the table redirected: it was bound for a daemon port at the
    gateway, and arrived on the listener for that same port. Anything else — a direct dial of the
    listener, a port no rule names — is refused."""
    return (
        dest is not None
        and dest[0] == gw
        and in_ranges(dest[1], ranges)
        and local is not None
        and local[1] == dest[1]
    )


def mac(key: bytes, fields: str) -> str:
    return hmac.new(key, fields.encode(), hashlib.sha256).hexdigest()


def stamp(key: bytes, items: list[tuple[str, str]]) -> str:
    """The header value: ``k=v`` pairs joined by ``; ``, then ``; mac=<hmac>`` over all of them.
    No value may hold ``;`` or whitespace (they are addresses, names and numbers)."""
    for k, v in items:
        if not re.fullmatch(r"[a-z]+", k) or not re.fullmatch(r"[^;\s]+", v):
            raise ValueError(f"unstampable field {k}={v!r}")
    fields = "; ".join(f"{k}={v}" for k, v in items)
    return f"{fields}; mac={mac(key, fields)}"


def is_request_head(head: bytes) -> bool:
    """A canonical HTTP/1 request head (a proxy request): CRLF line ends only, and a request line
    of an upper-case method, one space, the target, one space, the version. Only such a head is
    stamped; anything else on a proxy port is refused, never spliced: mitmproxy serves heads this
    rejects (it splits the request line on whitespace, takes any case of method, and ends a line
    at a bare LF), so splicing one would carry a container's request through unstamped."""
    if BARE_LF.search(head):
        return False
    return bool(REQUEST_LINE.match(head.split(b"\r\n", 1)[0]))


def restamp(head: bytes, value: str) -> tuple[bytes, int]:
    """``head`` (through its ``\\r\\n\\r\\n``) without any stamp line the client wrote, with ours
    appended last; and how many client lines were removed."""
    lines = head[: -4 if head.endswith(b"\r\n\r\n") else len(head)].split(b"\r\n")
    kept = [lines[0]]
    stripped = 0
    for line in lines[1:]:
        if line.split(b":", 1)[0].strip().lower() == HEADER:
            stripped += 1
        elif line:
            kept.append(line)
    kept.append(STAMP_HEADER.encode() + b": " + value.encode())
    return b"\r\n".join(kept) + b"\r\n\r\n", stripped


def classify(path: str, uid: int) -> tuple[str, str]:
    """A socket's cgroup path → ``(who, id)``: ``("container", <64 hex>)`` for the OUTERMOST
    ``libpod-<id>.scope`` under the VM user's ``user@<uid>.service`` (podman names that scope from
    an id it generated; a client can name the sub-cgroups below it and the slices above, so nothing
    else is read — ADR-0032), ``("build", "")`` for a build step's cgroup there
    (:data:`BUILD_SCOPE`), else ``("vm", "")``: the VM user's own processes, the podman service's
    pulls, anything else."""
    root = f"/user.slice/user-{uid}.slice/user@{uid}.service/"
    if not path.startswith(root):
        return "vm", ""
    parts = path[len(root) :].split("/")
    for name in parts:
        m = SCOPE.match(name)
        if m:
            return "container", m.group(1)
    if any(BUILD_SCOPE.match(name) for name in parts):
        return "build", ""
    return "vm", ""


def bridge_nft(gw: str, ranges: list[tuple[int, int]], listen: int) -> str:
    """The rootless namespace's table: a bridge container's connection to a gateway daemon port
    lands on the bridge listener. Replaced whole (the add-then-delete idiom makes a first load
    and a reload the same transaction): a restart never piles up duplicate rules. (No doubled
    braces anywhere in this file: Lima renders the boot script that carries it as a template.)"""
    o, c = "{", "}"
    return "\n".join([
        "table ip " + TABLE, "delete table ip " + TABLE,
        "table ip " + TABLE + " " + o,
        "  chain pre " + o,
        "    type nat hook prerouting priority dstnat - 10; policy accept;",
        "    ip daddr " + gw + " tcp dport " + o + " " + nft_ranges(ranges) + " " + c
        + " redirect to :" + str(listen),
        "  " + c,
        c, "",
    ])  # fmt: skip


def vm_nft(gw: str, ranges: list[tuple[int, int]], walled: str, addr: str) -> str:
    """The VM namespace's table. ``out`` sends the walled principals' connections to the gateway's
    daemon ports to the VM listener on the SAME port at ``addr``; ``guard`` refuses any of theirs
    that still names a daemon port after that, so nothing reaches the host around the forwarder. A
    drop in any table's base chain is final, so this holds whatever the VM firewall's own table
    accepts. The port is kept on purpose: every port DNATed onto one listener made a client port
    reused toward another daemon port clash with the earlier connection's conntrack entry, and
    conntrack remapped the source port — after which sock_diag can't find the client's socket."""
    o, c = "{", "}"
    match = ("meta skuid " + o + " " + walled + " " + c + " ip daddr " + gw + " tcp dport "
             + o + " " + nft_ranges(ranges) + " " + c)  # fmt: skip
    return "\n".join([
        "table ip " + TABLE, "delete table ip " + TABLE,
        "table ip " + TABLE + " " + o,
        "  chain out " + o,
        "    type nat hook output priority dstnat - 10; policy accept;",
        "    " + match + " dnat to " + addr,
        "  " + c,
        "  chain guard " + o,
        "    type filter hook output priority filter - 10; policy accept;",
        "    " + match + " counter reject with tcp reset"
        + ' comment "fy-fwd: only through the forwarder"',
        "  " + c,
        c, "",
    ])  # fmt: skip


def vm_route_cmds(gw: str, ranges: list[tuple[int, int]], uid_ranges: list[str]) -> list[list[str]]:
    """The policy routing that makes the redirect pass the VM firewall unchanged: the walled
    principals' connections to the gateway's daemon ports are routed out through ``lo``, which the
    firewall admits by its first rule. Every rule is deleted by priority first, so a re-run never
    duplicates one. Only those uids and ports: root (this forwarder's own upstream) and every other
    destination take the main table as before."""
    cmds = [["ip", "route", "replace", f"{gw}/32", "dev", "lo", "table", str(ROUTE_TABLE)]]
    prio = RULE_PRIORITY
    for uids in uid_ranges:
        for a, b in ranges:
            rule = ["ip", "rule", "add", "priority", str(prio), "uidrange", uids, "to", gw]
            cmds.append([*rule, "ipproto", "tcp", "dport", f"{a}-{b}", "lookup", str(ROUTE_TABLE)])
            prio += 1
    return cmds


def uid_ranges(walled: str) -> list[str]:
    """``"501, 524288-1074266111"`` (the nft spelling) → ``["501-501", "524288-1074266111"]``."""
    out = []
    for part in walled.replace(",", " ").split():
        lo, _, hi = part.partition("-")
        out.append(f"{int(lo)}-{int(hi or lo)}")
    return out


# ── sock_diag: who owns the socket at the other end of a VM-listener connection ───────

NETLINK_SOCK_DIAG = 4
SOCK_DIAG_BY_FAMILY = 20
NLMSG_ERROR = 2
NLMSG_DONE = 3
INET_DIAG_CGROUP_ID = 21
_NOCOOKIE = 0xFFFFFFFF


def diag_request(src: tuple[str, int], dst: tuple[str, int], seq: int = 1) -> bytes:
    """An exact ``SOCK_DIAG_BY_FAMILY`` lookup of the TCP socket whose local end is ``src`` and
    remote end ``dst`` (the client's socket: its remote end is still the gateway daemon port)."""
    sockid = struct.pack(
        "!HH16s16s", src[1], dst[1], socket.inet_aton(src[0]) + bytes(12),
        socket.inet_aton(dst[0]) + bytes(12),
    ) + struct.pack("=III", 0, _NOCOOKIE, _NOCOOKIE)  # fmt: skip
    req = struct.pack("=BBBBI", socket.AF_INET, socket.IPPROTO_TCP, 0, 0, 0xFFFFFFFF) + sockid
    return struct.pack("=IHHII", 16 + len(req), SOCK_DIAG_BY_FAMILY, 1, seq, 0) + req


def parse_diag(data: bytes) -> tuple[int, int, int | None] | None:
    """The reply → ``(uid, inode, cgroup id)``; ``None`` for no such socket (or an error)."""
    off = 0
    while off + 16 <= len(data):
        length, kind, _flags, _seq, _pid = struct.unpack_from("=IHHII", data, off)
        if length < 16:
            return None
        if kind in (NLMSG_ERROR, NLMSG_DONE):
            return None
        if kind == SOCK_DIAG_BY_FAMILY and length >= 16 + 72:
            body = off + 16
            uid, inode = struct.unpack_from("=II", data, body + 64)
            cgroup = None
            at = body + 72
            end = off + length
            while at + 4 <= end:
                alen, atype = struct.unpack_from("=HH", data, at)
                if alen < 4:
                    break
                if atype == INET_DIAG_CGROUP_ID and alen >= 12:
                    (cgroup,) = struct.unpack_from("=Q", data, at + 4)
                at += (alen + 3) & ~3
            return uid, inode, cgroup
        off += (length + 3) & ~3
    return None


def diag_lookup(src: tuple[str, int], dst: tuple[str, int]) -> tuple[int, int, int | None] | None:
    with socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_SOCK_DIAG) as s:
        s.settimeout(2)
        s.send(diag_request(src, dst))
        return parse_diag(s.recv(65536))


class Cgroups:
    """cgroup id (the cgroup2 directory's inode) → its path, from a walk of the hierarchy, cached
    and re-walked on a miss (a new container's scope)."""

    def __init__(self, root: str = "/sys/fs/cgroup") -> None:
        self.root = root
        self.paths: dict[int, str] = {}

    def _walk(self) -> None:
        paths = {}
        for here, _dirs, _files in os.walk(self.root):
            try:
                paths[os.stat(here).st_ino] = here[len(self.root) :] or "/"
            except OSError:
                continue
        self.paths = paths

    def path(self, cgroup_id: int) -> str | None:
        if cgroup_id not in self.paths:
            self._walk()
        return self.paths.get(cgroup_id)

    def owner_comm(self, path: str, inode: int, uids: list[tuple[int, int]],
                   proc: str = "/proc") -> str | None:  # fmt: skip
        """The command name of the process holding socket ``inode``: looked for first among the
        processes of cgroup ``path`` (the socket's own), then among every process the walled
        ``uids`` own. The second look matters: a socket keeps the cgroup it was MADE in, and pasta
        pre-opens a pool of sockets before podman moves it into its own scope, so a pasta socket
        can name a cgroup pasta is no longer in (seen live, 2026-10-23)."""
        target = f"socket:[{inode}]"
        try:
            with open(os.path.join(self.root + path, "cgroup.procs")) as f:
                first = f.read().split()
        except OSError:
            first = []
        rest = []
        for pid in os.listdir(proc):
            if pid.isdigit() and pid not in first:
                try:
                    uid = os.stat(f"{proc}/{pid}").st_uid
                except OSError:
                    continue
                if any(a <= uid <= b for a, b in uids):
                    rest.append(pid)
        for pid in first + rest:
            try:
                fds = os.listdir(f"{proc}/{pid}/fd")
            except OSError:
                continue  # the process exited
            for fd in fds:
                try:
                    if os.readlink(f"{proc}/{pid}/fd/{fd}") != target:
                        continue
                    with open(f"{proc}/{pid}/comm") as f:
                        return f.read().strip()
                except OSError:
                    continue  # that one fd closed since the listing: the rest still count
        return None


def owner(
    peer: tuple[str, int],
    dest: tuple[str, int],
    uid: int,
    walled: list[tuple[int, int]],
    cgroups: Cgroups,
    lookup=diag_lookup,
) -> tuple[str, str, int] | None:
    """Who is at the other end of a VM-listener connection: ``(who, id, socket uid)``, ``who`` per
    :func:`classify` plus ``"pasta"`` for a pasta/slirp helper; ``None`` when that can't be told
    (the caller refuses). Only a socket that classifies as the VM's own is looked up by process:
    a container's or a build's cgroup already says whose it is, and a pasta helper is never in
    one — it runs for podman, as the VM user."""
    found = lookup(peer, dest)
    if found is None or found[2] is None:
        return None
    sock_uid, inode, cgroup_id = found
    path = cgroups.path(cgroup_id)
    if path is None:
        return None
    who, cid = classify(path, uid)
    if who == "vm":
        comm = cgroups.owner_comm(path, inode, walled)
        if comm is None:
            return None  # held by no walled process we can find: never a guess
        if comm in PASTA:
            return "pasta", "", sock_uid
    return who, cid, sock_uid


# ── the guest side: namespaces, listeners, the relay ──────────────────────────────────


def log(**kw: object) -> None:
    kw["t"] = round(time.time(), 3)
    print(json.dumps(kw), flush=True)


def put(name: str, text: str) -> None:
    """One report file, replaced atomically, world-readable."""
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
    tmp = f"{STATE_DIR}/{name}.tmp"
    with open(tmp, "w") as fh:
        fh.write(text + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, f"{STATE_DIR}/{name}")


class Netns:
    """The VM user's rootless network namespace, reached through podman's pause process: the
    namespace file is bind-mounted only inside podman's own mount namespace, which root sees as
    ``/proc/<pause>/root/…``. An ordinary empty file means none is mounted."""

    def __init__(self, uid: int) -> None:
        self.uid = uid
        self.rel = f"run/user/{uid}/containers/networks/rootless-netns/rootless-netns"

    def ident(self) -> tuple[str, int] | None:
        try:
            with open(f"/run/user/{self.uid}/libpod/tmp/pause.pid") as f:
                pause = f.read().strip()
        except OSError:
            return None
        path = f"/proc/{pause}/root/{self.rel}"
        try:
            st = os.stat(path)
        except OSError:
            return None
        if st.st_dev != os.stat("/proc/self/ns/net").st_dev:
            return None  # not nsfs: the bind mount is gone
        return path, st.st_ino


def in_netns(path: str, fn):
    """``fn()`` with this thread in ``path``'s network namespace, then back."""
    home = os.open("/proc/self/ns/net", os.O_RDONLY)
    target = os.open(path, os.O_RDONLY)
    try:
        os.setns(target, CLONE_NEWNET)
        try:
            return fn()
        finally:
            os.setns(home, CLONE_NEWNET)
    finally:
        os.close(target)
        os.close(home)


def listener(addr: str, port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((addr, port))
    s.listen(1024)
    s.setblocking(False)
    return s


def interfaces() -> dict[str, str]:
    """This thread's namespace's IPv4 addresses → interface names."""
    import fcntl

    out = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for _idx, name in socket.if_nameindex():
            try:
                raw = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))
            except OSError:
                continue  # no IPv4 address
            out[socket.inet_ntoa(raw[20:24])] = name
    return out


def original_dst(sock: socket.socket) -> tuple[str, int] | None:
    try:
        raw = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
    except OSError:
        return None
    port, addr = struct.unpack_from("!2xH4s", raw)
    return socket.inet_ntoa(addr), port


async def read_head(reader: asyncio.StreamReader) -> tuple[bytes, bytes] | None:
    buf = b""
    while b"\r\n\r\n" not in buf:
        if BARE_LF.search(buf):  # not a canonical head, whatever follows: refused without waiting
            return buf, b""
        chunk = await asyncio.wait_for(reader.read(4096), timeout=HEAD_TIMEOUT)
        if not chunk:
            return None
        buf += chunk
        if len(buf) > MAX_HEAD:
            return None
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", rest


def send_queue(sock) -> int | None:
    """Bytes ``sock`` still holds for its peer, sent or not (``SIOCOUTQ``); ``None`` where that
    can't be read — which never counts as drained."""
    import fcntl

    try:
        raw = fcntl.ioctl(sock.fileno(), SIOCOUTQ, struct.pack("i", 0))
    except (OSError, ValueError):
        return None
    return struct.unpack("i", raw)[0]


async def drained(w: asyncio.StreamWriter, wait: float | None = None) -> bool:
    """Has the peer acknowledged every byte written to ``w``? Waits up to ``wait`` for it."""
    try:
        await w.drain()
    except (ConnectionError, OSError):
        return False
    sock = w.get_extra_info("socket")
    deadline = time.monotonic() + (DRAIN_WAIT if wait is None else wait)
    while True:
        queued = send_queue(sock) if sock is not None else None
        if queued == 0:
            return True
        if queued is None or time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.01)


class Forwarder:
    def __init__(self, a: argparse.Namespace, key: bytes, boot: str) -> None:
        self.a = a
        self.key = key
        self.boot = boot
        self.seq = 0
        self.proxy = parse_ports(a.proxy_ports)
        self.plain = parse_ports(a.plain_ports) if a.plain_ports else []
        self.cgroups = Cgroups()
        self.walled = [tuple(int(x) for x in r.split("-")) for r in uid_ranges(a.walled)]
        self.ifaces: dict[str, str] = {}
        self.netns_path: str | None = None
        self.stats = {"bridge": 0, "vm": 0, "refused": 0, "stripped": 0, "upstream_failed": 0,
                      "redials": 0, "aborted": 0}  # fmt: skip
        self.dial = None  # the upstream connect (a seam): asyncio.open_connection, looked up late

    def ports(self) -> list[tuple[int, int]]:
        return self.proxy + self.plain

    def next_stamp(self, items: list[tuple[str, str]]) -> str:
        self.seq += 1
        return stamp(self.key, [("v", "1"), *items, ("boot", self.boot), ("seq", str(self.seq))])

    async def handle_bridge(self, reader, writer) -> None:
        peer = writer.get_extra_info("peername")
        local = writer.get_extra_info("sockname")
        dest = original_dst(writer.get_extra_info("socket"))
        if dest is None or not in_ranges(dest[1], self.ports()):
            return self.refuse(writer, "bridge listener dialled directly", peer)
        if local[0] not in self.ifaces and self.netns_path:
            with contextlib.suppress(OSError):
                self.ifaces = in_netns(self.netns_path, interfaces)
        net = self.ifaces.get(local[0], "unknown")
        self.stats["bridge"] += 1
        await self.relay(reader, writer, dest, [("via", "bridge"), ("src", f"{peer[0]}:{peer[1]}"),
                                                ("net", net)])  # fmt: skip

    async def handle_vm(self, reader, writer) -> None:
        peer = writer.get_extra_info("peername")
        dest = original_dst(writer.get_extra_info("socket"))
        if not vm_dest_ok(dest, writer.get_extra_info("sockname"), self.ports(), self.a.gw):
            return self.refuse(writer, "VM listener dialled directly", peer)
        try:
            who = await asyncio.to_thread(owner, peer, dest, self.a.uid, self.walled, self.cgroups)
        except OSError:
            who = None
        if who is None:
            return self.refuse(writer, "owner unknown", peer)
        kind, cid, sock_uid = who
        if kind == "pasta":
            # Some container's traffic, re-made by its pasta helper: whose can't be told here.
            return self.refuse(writer, "a pasta helper (attach the container to a bridge)", peer)
        items = [("via", "vm"), ("src", f"{peer[0]}:{peer[1]}"), ("who", kind)]
        if cid:
            items.append(("ctr", cid))
        items.append(("uid", str(sock_uid)))
        self.stats["vm"] += 1
        await self.relay(reader, writer, dest, items)

    def refuse(self, writer, why: str, peer) -> None:
        self.stats["refused"] += 1
        log(ev="refused", why=why, peer=f"{peer[0]}:{peer[1]}" if peer else "")
        writer.close()

    async def relay(self, reader, writer, dest: tuple[str, int], items) -> None:
        rest = b""
        if in_ranges(dest[1], self.proxy):
            try:
                got = await read_head(reader)
            except (TimeoutError, ConnectionError, OSError):
                got = None
            if got is None:
                writer.close()
                return
            head, rest = got
            if not is_request_head(head):
                why = "not a canonical HTTP/1 proxy request"
                return self.refuse(writer, why, writer.get_extra_info("peername"))
            head, stripped = restamp(head, self.next_stamp(items))
            self.stats["stripped"] += stripped
            rest = head + rest
        try:
            ur, uw = await self.open_upstream(dest)
        except (OSError, TimeoutError) as e:
            self.stats["upstream_failed"] += 1
            log(ev="upstream-failed", port=dest[1], err=str(e) or type(e).__name__)
            writer.close()
            return
        # Which side closed first decides how the upstream socket is closed. The upstream closing
        # first (the proxy after `Connection: close`, an error, a refusal, an idle timeout) is a
        # host-first close, which leaves gVisor's end of the tuple in TIME_WAIT for 60 s, and a
        # later SYN reusing it is silently dropped (google/gvisor#15013). So then the forwarder
        # never sends its FIN: once the client is done too and everything owed upstream is
        # acknowledged, it closes with an RST, which gVisor takes before it enters TIME_WAIT
        # (docs/lima-usernet-freeze.md, Finding 6). The client finishing first is a FIN as before:
        # the guest then holds TIME_WAIT itself, and nothing needs avoiding.
        upstream_done = False  # the upstream's EOF was read
        fin_sent = False  # we half-closed upstream (the client finished first)

        async def to_upstream() -> None:
            nonlocal fin_sent
            try:
                if rest:
                    uw.write(rest)
                while data := await reader.read(256 * 1024):
                    uw.write(data)
                    await uw.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                if not upstream_done:
                    try:
                        uw.write_eof()
                        fin_sent = True
                    except (OSError, RuntimeError):
                        pass

        async def to_client() -> None:
            nonlocal upstream_done
            try:
                while data := await ur.read(256 * 1024):
                    writer.write(data)
                    await writer.drain()
                upstream_done = True
            except (ConnectionError, OSError):
                pass
            finally:
                try:
                    if writer.can_write_eof():
                        writer.write_eof()
                    else:
                        writer.close()
                except (OSError, RuntimeError):
                    writer.close()

        try:
            await asyncio.gather(to_upstream(), to_client())
        finally:
            # Each direction only half-closed: close both sockets, or every connection leaves two
            # descriptors open until a garbage collection reaches them.
            writer.close()
            if upstream_done and not fin_sent and await drained(uw):
                sock = uw.get_extra_info("socket")
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    self.stats["aborted"] += 1
            uw.close()

    async def open_upstream(self, dest: tuple[str, int]):
        """Connect upstream, re-dialling on a new socket when a connect is slow
        (:data:`CONNECT_TIMEOUT`, at most :data:`CONNECT_ATTEMPTS` tries). A refusal or any other
        error is an answer, not a lost SYN: it fails at once."""
        dial = self.dial or asyncio.open_connection
        for attempt in range(CONNECT_ATTEMPTS):
            try:
                return await asyncio.wait_for(dial(*dest), timeout=CONNECT_TIMEOUT)
            except TimeoutError:
                if attempt + 1 < CONNECT_ATTEMPTS:
                    self.stats["redials"] += 1
        raise TimeoutError(f"no answer to {CONNECT_ATTEMPTS} connects of {CONNECT_TIMEOUT:g}s")


class Follower:
    """Keeps a bridge listener in the CURRENT rootless namespace. The old listener is held until
    a new namespace appears: while it is held its namespace can't be freed, so its inode can't be
    handed to the next one, and an inode comparison stays a sound re-creation test (they ARE
    reused once released — spike finding 2)."""

    def __init__(self, fwd: Forwarder, netns: Netns) -> None:
        self.fwd = fwd
        self.netns = netns
        self.held: tuple[str, int] | None = None
        self.announced = ""
        self.server: asyncio.AbstractServer | None = None

    def load(self, path: str) -> None:
        a = self.fwd.a
        text = bridge_nft(a.gw, self.fwd.ports(), a.bridge_port)
        res = subprocess.run(["nsenter", f"--net={path}", "nft", "-f", "-"], input=text,
                             text=True, capture_output=True, timeout=10)  # fmt: skip
        if res.returncode != 0:
            raise OSError(f"nft in the rootless namespace: {res.stderr.strip()}")

    async def tick(self) -> None:
        cur = self.netns.ident()
        if cur is None:
            if self.announced != "none":
                put("netns", "none")  # the old listener stays held (see the class docstring)
                self.announced = "none"
            return
        if cur == self.held:
            return
        t0 = time.monotonic()
        path, ino = cur
        self.load(path)
        sock = in_netns(path, lambda: listener("0.0.0.0", self.fwd.a.bridge_port))
        old, self.server = (
            self.server,
            await asyncio.start_server(self.fwd.handle_bridge, sock=sock, backlog=1024),
        )
        self.fwd.netns_path = path
        with contextlib.suppress(OSError):
            self.fwd.ifaces = in_netns(path, interfaces)
        self.held = cur
        if old is not None:
            old.close()
        put("netns", str(ino))
        self.announced = str(ino)
        log(ev="netns-followed", ino=ino, ms=round((time.monotonic() - t0) * 1000, 1))

    async def run(self, every: float) -> None:
        said = ""
        stats = ""
        while True:
            now = json.dumps(self.fwd.stats, sort_keys=True)
            if now != stats and int(time.monotonic() / every) % 50 == 0:
                put("stats", now)  # every ~5 s at most, and only when something moved
                stats = now
            try:
                await self.tick()
                if said:
                    put("state", f"serving {self.fwd.a.id}")
                said = ""
            except Exception as e:  # keep following: the next tick may find a usable namespace
                if str(e) != said:
                    log(ev="follow-failed", err=str(e))
                    put("state", f"degraded: following the rootless namespace failed: {e}")
                said = str(e)
            await asyncio.sleep(every)


def setup_vm(a: argparse.Namespace, ports: list[tuple[int, int]]) -> None:
    """The VM namespace's table, then its policy routes (table first: routes without the table
    would route the walled principals' connections to ``lo`` with nothing to catch them)."""
    res = subprocess.run(["nft", "-f", "-"], input=vm_nft(a.gw, ports, a.walled, a.vm_addr),
                         text=True, capture_output=True, timeout=10)  # fmt: skip
    if res.returncode != 0:
        raise OSError(f"nft: {res.stderr.strip()}")
    for prio in range(RULE_PRIORITY, RULE_PRIORITY + 64):
        subprocess.run(["ip", "rule", "del", "priority", str(prio)], capture_output=True)
    for cmd in vm_route_cmds(a.gw, ports, uid_ranges(a.walled)):
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise OSError(f"{' '.join(cmd)}: {res.stderr.strip()}")


async def main(a: argparse.Namespace) -> None:
    with open(a.key, "rb") as f:
        key = f.read().strip()
    with open("/proc/sys/kernel/random/boot_id") as f:
        boot = f.read().strip().replace("-", "")[:12]
    ipaddress.IPv4Address(a.vm_addr)  # refuse a malformed address before any rule names it
    fwd = Forwarder(a, key, boot)
    setup_vm(a, fwd.ports())
    # One listener per daemon port (see vm_nft), on an address Lima's port forwarding ignores.
    servers = [
        await asyncio.start_server(fwd.handle_vm, sock=listener(a.vm_addr, port), backlog=1024)
        for port in each_port(fwd.ports())
    ]
    follower = Follower(fwd, Netns(a.uid))
    put("state", f"serving {a.id}")
    log(ev="started", id=a.id, vm=f"{a.vm_addr} ({len(servers)} ports)", bridge=a.bridge_port)
    await follower.run(a.poll)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="fy-fwd")
    p.add_argument("--id", default="")
    p.add_argument("--uid", type=int, required=True)
    p.add_argument("--walled", required=True, help="the walled uids, nft spelling")
    p.add_argument("--gw", required=True)
    p.add_argument("--proxy-ports", required=True)
    p.add_argument("--plain-ports", default="")
    p.add_argument("--key", default="/etc/fy-fwd/forwarder.key")
    p.add_argument("--bridge-port", type=int, default=4748)
    p.add_argument("--vm-addr", default="127.42.0.1")
    p.add_argument("--poll", type=float, default=0.1)
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    if os.geteuid() != 0:
        sys.exit("fy-fwd must run as root")
    try:
        asyncio.run(main(args))
    except Exception as e:
        put("state", f"failed: {type(e).__name__}: {e}")
        raise
