"""fy-monitor relay — the guest monitor's root relay (ADR-0031). Runs IN THE GUEST as root, under
systemd (``fy-monitor-relay.service``), from the copy the monitor boot script wrote — foldyard's
own packaged code, never anything from the repo mount. Stdlib only: it runs under the guest's
``/usr/bin/python3``.

It does three things and nothing else:

1. **Tails Tetragon's export** (``/var/log/tetragon/tetragon.log``), across its rotations.
2. **Writes a spool** the host pulls over ssh as the VM user: ``/var/lib/fy-monitor/spool/
   <epoch>-<boot id>/<first seq>.jsonl``, root-owned and world-readable, one line per record:
   ``<seq> <hmac> <json>``. The HMAC (SHA-256, over ``<boot>:<seq>:<json>``) uses a key only root
   can read (``/etc/fy-monitor/relay.key``, written at boot from the host's recording), so the
   host can tell a line this relay wrote from one anything else wrote, and the per-boot sequence
   shows what went missing. The VM user can read the spool — it is the transport — but not write
   it; whatever sits between it and the host can drop or delay lines, never forge them.
3. **Snapshots which container owns which mount namespace**, as kernel facts: at start, every
   ``SNAPSHOT_EVERY`` seconds, and as soon as an event carries a mount namespace the last
   snapshot didn't have (at most once a second). It never joins events to containers itself —
   the host does that, so attribution improves with a foldyard upgrade, not a VM restart, and
   the raw evidence stays re-derivable.

The snapshot walks the VM user's delegated cgroup subtree top-down: the OUTERMOST
``libpod-<64 hex>.scope`` on each path owns everything below it. podman names that scope from an
id it generated; an engine client CAN name the sub-cgroup below it (``run.oci.systemd.subgroup``)
and the slices above it (``--cgroup-parent``), so no name below a scope is ever read
(docs/ebpf-monitoring-spike.md, finding 5).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import time

SRC = "/var/log/tetragon/tetragon.log"
LIB = "/var/lib/fy-monitor"
SPOOL = LIB + "/spool"
POS = LIB + "/relay.pos"
KEY = "/etc/fy-monitor/relay.key"
STATE = "/run/fy-monitor/relay"

SEGMENT_BYTES = 4 << 20  # roll the spool file past this
SPOOL_BYTES = 64 << 20  # keep at most this much spool, oldest dropped first (the host pulls often)
SNAPSHOT_EVERY = 60.0
NEW_INODE_MIN_GAP = 1.0
SCOPE = re.compile(r"^libpod-([0-9a-f]{64})\.scope$")


# ── pure parts (unit-tested on the host) ──────────────────────────────────────────────


def mac(key: bytes, boot: str, seq: int, body: str) -> str:
    return hmac.new(key, f"{boot}:{seq}:{body}".encode(), hashlib.sha256).hexdigest()


def spool_line(key: bytes, boot: str, seq: int, record: dict) -> str:
    body = json.dumps(record, separators=(",", ":"), sort_keys=True)
    return f"{seq} {mac(key, boot, seq, body)} {body}\n"


def mnt_inode(event: dict) -> int | None:
    """The mount-namespace inode of the process an event is about (``enable-process-ns``)."""
    for kind, body in event.items():
        if kind.startswith("process_") and isinstance(body, dict):
            ns = (body.get("process") or {}).get("ns") or {}
            inum = (ns.get("mnt") or {}).get("inum")
            return int(inum) if inum else None
    return None


def scopes(user_root: str) -> dict[str, str]:
    """``{scope dir: container id}`` for the outermost podman scope on every path under
    ``user_root`` (the VM user's ``user@<uid>.service``)."""
    out = {}
    for root, dirs, _files in os.walk(user_root):
        for name in list(dirs):
            m = SCOPE.match(name)
            if m:
                out[os.path.join(root, name)] = m.group(1)
                dirs.remove(name)  # never below a scope: those names are the client's
    return out


def members(scope_dir: str) -> list[int]:
    pids = []
    for root, _dirs, files in os.walk(scope_dir):
        if "cgroup.procs" in files:
            try:
                with open(os.path.join(root, "cgroup.procs")) as f:
                    pids += [int(x) for x in f.read().split()]
            except OSError:
                continue  # the cgroup went away mid-walk
    return pids


def ns_inode(pid: int | str, proc: str = "/proc") -> int | None:
    try:
        link = os.readlink(f"{proc}/{pid}/ns/mnt")
    except OSError:
        return None  # exited between the walk and the read
    return int(link[link.index("[") + 1 : -1])


def snapshot(user_root: str, proc: str = "/proc") -> dict:
    """``{"vm": <the VM's own mount namespace>, "map": {inode: [container ids]}}`` — more than one
    id for an inode is reported as is: the host calls it ambiguous, never picks one."""
    by_inode: dict[int, set[str]] = {}
    for scope_dir, cid in scopes(user_root).items():
        for pid in members(scope_dir):
            inode = ns_inode(pid, proc)
            if inode is not None:
                by_inode.setdefault(inode, set()).add(cid)
    return {
        "vm": ns_inode(1, proc),
        "map": {str(k): sorted(v) for k, v in sorted(by_inode.items())},
    }


# ── the spool ─────────────────────────────────────────────────────────────────────────


class Spool:
    """Per-boot segments, the newest appended to; total size bounded across boots."""

    def __init__(self, key: bytes, boot: str, boot_dir: str, seq: int):
        self.key, self.boot, self.seq = key, boot, seq
        self.dir = os.path.join(SPOOL, boot_dir)
        os.makedirs(self.dir, mode=0o755, exist_ok=True)
        os.chmod(self.dir, 0o755)
        self.f = None
        self._open(new=False)

    def _open(self, new: bool) -> None:
        if self.f:
            self.f.close()
        segs = sorted(os.listdir(self.dir))
        name = segs[-1] if segs and not new else f"{self.seq:012d}.jsonl"
        self.path = os.path.join(self.dir, name)
        self.f = open(self.path, "a")
        os.chmod(self.path, 0o644)

    def write(self, record: dict) -> int:
        seq = self.seq
        self.f.write(spool_line(self.key, self.boot, seq, record))
        self.seq += 1
        if self.f.tell() >= SEGMENT_BYTES:
            self.f.flush()
            self._open(new=True)
            self._prune()
        return seq

    def flush(self) -> None:
        self.f.flush()

    def _prune(self) -> None:
        files = []
        for d in sorted(os.listdir(SPOOL)):
            for s in sorted(os.listdir(os.path.join(SPOOL, d))):
                p = os.path.join(SPOOL, d, s)
                files.append((p, os.path.getsize(p)))
        total = sum(size for _, size in files)
        for p, size in files:
            if total <= SPOOL_BYTES or p == self.path:
                break
            os.unlink(p)
            total -= size
        for d in os.listdir(SPOOL):
            full = os.path.join(SPOOL, d)
            if full != self.dir and not os.listdir(full):
                os.rmdir(full)


# ── the loop ──────────────────────────────────────────────────────────────────────────


def _state(text: str) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE + ".tmp", "w") as f:
        f.write(text + "\n")
    os.chmod(STATE + ".tmp", 0o644)
    os.replace(STATE + ".tmp", STATE)


def _load_pos() -> dict:
    try:
        with open(POS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_pos(pos: dict) -> None:
    with open(POS + ".tmp", "w") as f:
        json.dump(pos, f)
    os.replace(POS + ".tmp", POS)


def main(uid: int) -> None:
    with open(KEY, "rb") as f:
        key = f.read().strip()
    with open("/proc/sys/kernel/random/boot_id") as f:
        boot = f.read().strip()
    user_root = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service"
    os.makedirs(SPOOL, mode=0o755, exist_ok=True)
    os.chmod(SPOOL, 0o755)

    pos = _load_pos()
    same_boot = pos.get("boot") == boot
    boot_dir = pos["dir"] if same_boot else f"{int(time.time()):010d}-{boot}"
    spool = Spool(key, boot, boot_dir, pos.get("seq", 0) if same_boot else 0)
    spool.write({"k": "start", "t": time.time(), "resumed": same_boot})

    while not os.path.exists(SRC):
        _state("waiting for tetragon's export")
        time.sleep(2)
    src = open(SRC, "rb")  # bytes: offsets must be real byte positions
    ino = os.fstat(src.fileno()).st_ino
    if pos.get("ino") == ino:
        src.seek(pos.get("off", 0))
    elif pos:
        # The export rotated (or was replaced) while the relay was down: what was written in
        # between is in a rotated file this relay doesn't read. Say so, never imply continuity.
        spool.write({"k": "gap", "t": time.time(), "why": "tetragon's export rotated unseen"})

    known: set[str] = set()
    tried: set[int] = set()  # new inodes a snapshot already looked for (cleared by the timer)
    pending: set[int] = set()  # new inodes waiting for the next snapshot
    last_snap = 0.0
    last_save = time.monotonic()

    def take(why: str) -> None:
        nonlocal known, last_snap
        snap = snapshot(user_root)
        spool.write({"k": "scopes", "t": time.time(), "why": why, **snap})
        known = set(snap["map"]) | {str(snap["vm"])}
        last_snap = time.monotonic()

    def save() -> None:
        # Under a steady stream the loop never idles, so the position is saved on a clock too:
        # a crash then replays at most a couple of seconds, never the whole export.
        nonlocal last_save
        spool.flush()
        _save_pos({"boot": boot, "dir": boot_dir, "seq": spool.seq, "ino": ino,
                   "off": src.tell()})  # fmt: skip
        last_save = time.monotonic()

    take("start")
    while True:
        # The timer is checked on EVERY pass, not only when the export runs dry: under steady load
        # the loop never idles, and a relay that only snapshotted when idle shipped none at all —
        # leaving the host nothing to attribute with (seen live, 2026-10-04).
        if time.monotonic() - last_snap >= SNAPSHOT_EVERY:
            take("timer")
            tried.clear()
        if pending and time.monotonic() - last_snap >= NEW_INODE_MIN_GAP:
            take("new " + " ".join(str(i) for i in sorted(pending)))
            tried.update(pending)  # one look each: an inode no scope owns stays unmapped
            pending.clear()
        if time.monotonic() - last_save >= 2.0:
            save()
        line = src.readline()
        if line and not line.endswith(b"\n"):  # a half-written record: come back for the rest
            src.seek(-len(line), os.SEEK_CUR)
            line = b""
        if not line:
            save()
            _state(f"running seq {spool.seq}")
            try:
                if os.stat(SRC).st_ino != ino:  # rotated: the old fd is drained, follow the new
                    src.close()
                    src = open(SRC, "rb")
                    ino = os.fstat(src.fileno()).st_ino
                    continue
            except OSError:
                pass
            time.sleep(0.5)
            continue
        try:
            event = json.loads(line)
        except ValueError:
            spool.write({"k": "bad", "t": time.time(), "why": "unparseable tetragon line"})
            continue
        spool.write({"k": "ev", "e": event})
        inode = mnt_inode(event)
        if inode is not None and str(inode) not in known and inode not in tried:
            pending.add(inode)


if __name__ == "__main__":
    main(int(sys.argv[1]))
