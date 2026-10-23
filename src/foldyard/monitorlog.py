"""monitorlog.py — the guest monitor's events on the host (ADR-0032, slice 2).

Two halves, deliberately apart:

* **The pull** (:func:`pull_once`, run on a thread of the host supervisor by :func:`run`):
  fetches what the guest relay (``assets/monitor/relay.py``) spooled since the last pull, over
  the backend's ssh as the VM user, in BYTES (a decoded stream would make every byte offset after
  the first multi-byte character wrong). Each line is checked against the relay key
  (:func:`monitor.relay_key`) and its per-boot sequence, then appended — verified records only,
  with what was missing or forged recorded as such — to a bounded store under the state dir. The
  host never trusts the transport: whatever sits between the root relay and here (the VM user's
  ssh session included) can drop or delay lines, and the sequence shows it; it can't forge them.
  New container ids are described by asking the engine once (name, compose project) and that is
  stored too — as a CLAIM: names and labels are whatever the container's creator set. The store
  itself — segments, compression, retention, the catalogue — is :mod:`foldyard.monitorstore`.

* **The join** (:func:`attribute`, pure): which container did each event. The relay ships each
  event's mount-namespace inode and, separately, kernel snapshots of inode → container; the join
  happens here, at read time, over the stored evidence — so it can be improved and re-run without
  touching the VM. See :func:`attribute` for the rules, including why a namespace number on its
  own is never trusted across time. Any stretch of the store is joined with the catalogue's
  snapshots around it (:func:`join`), never just the ones that happen to fall inside the bytes
  read: two snapshots can be a minute and tens of megabytes apart under load.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config, monitorstore
from .sandbox import SshBackend, _ssh_argv

SPOOL = "/var/lib/fy-monitor/spool"
FETCH_BUDGET = 8_000_000  # bytes per pull; a full one is followed by another at once
PULL_EVERY = 5.0


def store_path() -> Path:
    """The live segment of the store (see :mod:`foldyard.monitorstore`)."""
    return monitorstore.live_path()


def _cursor_path() -> Path:
    return config.state_dir() / "monitor-cursor.json"


# ── the cursor: where the host has read up to, and how it went ───────────────────────────


@dataclass
class Cursor:
    path: str = ""  # "<epoch>-<boot>/<first seq>.jsonl", relative to the spool
    off: int = 0
    last: dict[str, int] = field(default_factory=dict)  # boot -> last seq stored
    described: list[str] = field(default_factory=list)  # container ids already asked about
    undescribed: list[str] = field(default_factory=list)  # seen, the engine not yet reachable
    pulled: float = 0.0  # last successful pull (host time)
    error: str = ""  # the last pull's failure, "" when it worked
    stored: int = 0
    gaps: int = 0  # sequence numbers that never arrived
    forged: int = 0  # lines that failed the HMAC
    behind: bool = False  # the last pull filled its budget: more is waiting

    @classmethod
    def load(cls) -> Cursor:
        try:
            raw = json.loads(_cursor_path().read_text())
            return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self) -> None:
        path = _cursor_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self)))
        tmp.replace(path)


# ── the pull ──────────────────────────────────────────────────────────────────────────


def fetch_script(cursor_path: str, cursor_off: int, budget: int = FETCH_BUDGET) -> str:
    """Run in the guest as the VM user: every spool segment at or after the cursor, from the
    cursor's offset, each as ``@@ <path> <offset> <bytes>`` then exactly that many bytes, at most
    ``budget`` bytes in all. Segment paths sort chronologically by construction (an epoch-prefixed
    boot dir, then the zero-padded first seq), so a string compare is the cursor test. Nothing in
    it writes: the VM user can't, and doesn't need to."""
    cur, off = shlex.quote(cursor_path), int(cursor_off)
    return f"""
cd {SPOOL} 2>/dev/null || exit 0
budget={int(budget)}
find . -mindepth 2 -maxdepth 2 -name '*.jsonl' | sort | while IFS= read -r p; do
  p=${{p#./}}
  [[ "$p" < {cur} ]] && continue
  off=0; [[ "$p" == {cur} ]] && off={off}
  size=$(stat -c %s "$p" 2>/dev/null) || continue
  [ "$size" -gt "$off" ] || continue
  n=$((size - off)); [ "$n" -gt "$budget" ] && n=$budget
  printf '@@ %s %s %s\\n' "$p" "$off" "$n"
  tail -c +$((off + 1)) "$p" | head -c "$n"
  budget=$((budget - n)); [ "$budget" -gt 0 ] || break
done
"""


def parse_fetch(out: bytes) -> list[tuple[str, int, bytes]]:
    """``[(path, offset, bytes)]`` from :func:`fetch_script`'s output. A chunk shorter than its
    header promised (the segment shrank — pruned mid-read) is kept as far as it goes."""
    chunks, i = [], 0
    while i < len(out):
        nl = out.find(b"\n", i)
        if nl < 0 or not out.startswith(b"@@ ", i):
            break
        _, path, off, n = out[i:nl].decode().split(" ")
        start = nl + 1
        data = out[start : start + int(n)]
        chunks.append((path, int(off), data))
        i = start + len(data)
    return chunks


def verify(key: bytes, boot: str, line: bytes) -> tuple[int, dict] | None:
    """``(seq, record)`` for a line the relay signed with ``key``, else ``None``."""
    try:
        seq_s, mac, body = line.decode().split(" ", 2)
        seq = int(seq_s)
    except ValueError:
        return None
    want = hmac.new(key, f"{boot}:{seq}:{body}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, want):
        return None
    try:
        return seq, json.loads(body)
    except ValueError:
        return None


def ingest(cur: Cursor, key: bytes, chunks: list[tuple[str, int, bytes]], now: float) -> list[dict]:
    """The store records for ``chunks``, advancing ``cur`` past every COMPLETE line (a trailing
    half-line — the relay mid-write, or the fetch budget — is left for the next pull)."""
    out: list[dict] = []
    for path, off, data in chunks:
        boot = path.split("/", 1)[0].split("-", 1)[-1]
        complete = data[: data.rfind(b"\n") + 1]
        for line in complete.splitlines():
            got = verify(key, boot, line)
            if got is None:
                cur.forged += 1
                out.append({"k": "forged", "boot": boot, "path": path, "recv": now})
                continue
            seq, rec = got
            last = cur.last.get(boot, -1)
            if seq <= last:
                continue  # already stored (a re-read after a crash between store and cursor)
            if seq > last + 1:
                cur.gaps += seq - last - 1
                out.append({"k": "gap", "boot": boot, "from": last + 1, "to": seq - 1, "recv": now})
            cur.last[boot] = seq
            out.append({**rec, "boot": boot, "seq": seq, "recv": now})
        if complete or path != cur.path:
            cur.path, cur.off = path, off + len(complete)
    return out


def _append(records: list[dict]) -> None:
    monitorstore.append(records)


def _engine_describe(ids: list[str]) -> dict[str, dict | None] | None:
    """What the engine says about ``ids``: name + compose project/service — CLAIMS, whatever the
    creator set. An id the engine no longer has maps to ``None`` (a short-lived container already
    gone: the box's own business, in the operator's reading).

    ``None`` overall when the engine couldn't be asked: the call failed, or it listed NO
    containers while the guest's own snapshot shows ``ids`` running — that is the wrong engine
    (an unresolved socket answers from the host's own empty store), never "all gone". Recording
    that as gone would stick: described ids aren't asked about again (seen live, 2026-10-04)."""
    from . import devmode  # import-light module; only the pull needs the engine env

    try:
        res = subprocess.run(
            [config.engine(), "ps", "-a", "--no-trunc", "--format",
             "{{.ID}}\t{{.Names}}\t{{json .Labels}}"],
            capture_output=True, text=True, timeout=15, env=devmode._engine_env(),
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    known: dict[str, dict] = {}
    for line in res.stdout.splitlines():
        cid, _, rest = line.partition("\t")
        name, _, raw = rest.partition("\t")
        labels = devmode._parse_labels(raw)
        known[cid] = {
            "name": name,
            "project": labels.get("com.docker.compose.project", ""),
            "service": labels.get("com.docker.compose.service", ""),
        }
    if not known:
        return None
    return {cid: known.get(cid) for cid in ids}


def _fetch_argv(target) -> list[str]:
    """The backend's ssh argv, MULTIPLEXED over one persistent connection. Every fresh login is
    itself watched activity in the guest — sshd reads ``authorized_keys``, PAM reads
    ``/etc/shadow`` — so a new connection per pull made the monitor record its own transport,
    about 260 events a pull (seen live, 2026-10-04). The master outlives a pull by five minutes;
    ``%C`` keeps the socket path short and per-target."""
    argv = _ssh_argv(target)
    control = Path.home() / ".foldyard" / "cm-%C"
    control.parent.mkdir(parents=True, exist_ok=True)
    opts = ["-o", "ControlMaster=auto", "-o", f"ControlPath={control}", "-o", "ControlPersist=300"]
    return [*argv[:-1], *opts, argv[-1]]


def _fetch(target, script: str) -> bytes:
    res = subprocess.run(
        [*_fetch_argv(target), f"bash -c {shlex.quote(script)}"], capture_output=True, timeout=60
    )
    if res.returncode != 0:
        raise OSError(res.stderr.decode(errors="replace").strip() or f"exit {res.returncode}")
    return res.stdout


def pull_once(
    backend: SshBackend, name: str, key: str, now: Callable[[], float] = time.time
) -> int:
    """One pull: fetch → verify → store → describe new containers → advance the cursor. Returns
    the number of records stored; failures are recorded on the cursor (the doctor row reads it)
    and re-raised for the caller to log."""
    cur = Cursor.load()
    try:
        target = backend.ssh_target(name)
        if target is None:
            raise OSError("no ssh route into the VM")
        chunks = parse_fetch(_fetch(target, fetch_script(cur.path, cur.off)))
        cur.behind = sum(len(data) for _, _, data in chunks) >= FETCH_BUDGET
        t = now()
        # the relay keys its HMAC with the key file's text (the hex string), so the host does too
        records = ingest(cur, key.encode(), chunks, t)
        seen = {cid for r in records if r.get("k") == "scopes" for ids in r["map"].values()
                for cid in ids}  # fmt: skip
        new_ids = sorted((seen | set(cur.undescribed)) - set(cur.described))
        if new_ids:
            described = _engine_describe(new_ids)
            if described is None:  # unreachable: ask again on the next pull, snapshot or not
                cur.undescribed = new_ids[-2000:]
            else:
                records.append({"k": "engine", "recv": t, "containers": described})
                cur.described = (cur.described + new_ids)[-2000:]
                cur.undescribed = []
        if records:
            _append(records)
        cur.stored += len(records)
        cur.pulled, cur.error = t, ""
        cur.save()
        return len(records)
    except Exception as e:
        cur.error = f"{type(e).__name__}: {e}"[:300]
        cur.save()
        raise


def run(log: Callable[[str], None], stopping: Callable[[], bool]) -> None:
    """The supervisor's pull loop (a daemon thread): every :data:`PULL_EVERY` seconds while the
    monitor is wanted and the VM runs. Logs a failure once per distinct message, never raises."""
    from . import machine, monitor

    said = ""
    while not stopping():
        msg = ""
        behind = False
        try:
            if monitor.wanted() and machine.state() == "running":
                pull_once(machine.BACKEND, machine.MACHINE, monitor.relay_key())
                behind = Cursor.load().behind
        except Exception as e:
            msg = f"guest monitor: pulling events failed: {e}"
        if msg and msg != said:
            log(msg)
        said = msg
        if behind:
            continue  # catching up: the guest's spool is bounded, a backlog left there is lost
        for _ in range(int(PULL_EVERY / 0.2)):
            if stopping():
                return
            time.sleep(0.2)


def start(log: Callable[[str], None], stopping: Callable[[], bool]) -> threading.Thread:
    t = threading.Thread(target=run, args=(log, stopping), name="monitor-pull", daemon=True)
    t.start()
    return t


# ── the join ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Event:
    """One normalised event (the schema `fy monitor` prints). ``who`` is how it was attributed
    (:func:`attribute`), each from kernel facts the box can't fake:

    * ``container``: a kernel snapshot names its scope, within that container's life;
    * ``build``: an image build's step (an init podman itself started in its own namespace);
    * ``podman``: podman's own rootless namespace — conmon, pasta, crun, netavark, the API service;
    * ``vm``: the VM itself — its own mount namespace, or its own user namespace (systemd's
      sandboxed services);
    * ``ambiguous``: the records disagree — two containers for one namespace, or a container's
      scope holding a process in the VM's or podman's namespace — never resolved by guessing;
    * ``unattributed``: none of these — a container too short-lived for any snapshot, a process
      Tetragon never saw start, podman's tools in a namespace of their own for an instant.

    ``crossed``: the process is in the VM's or podman's namespace, but its parent was in one that
    isn't — something crossed out of a container. Attributed to where it came from, and never left
    out by a hook's ``who`` filter."""

    time: str
    boot: str
    seq: int
    kind: str  # exec | exit | connect | file | other
    binary: str
    args: str
    detail: str
    uid: int | None
    pid: int | None
    who: str
    container: str = ""  # the 64-hex id, when who == "container"
    name: str = ""  # engine-reported (a claim)
    worktree: str = ""  # derived from the engine-reported compose project / box name (a claim)
    # Ancestry, as Tetragon reports it in every process body (additive: older records default).
    # Attribution names the container; ancestry tells its own daemons from what a session ran —
    # in BashArena the firewall daemon's reload runs `iptables -F`, which a time-window join blamed
    # on whichever tool call was running. ``parent``/``parent_args`` are the one hop;
    # ``exec_id``/``parent_exec_id`` rebuild the whole tree from exec events.
    ppid: int | None = None
    parent: str = ""
    parent_args: str = ""
    exec_id: str = ""
    parent_exec_id: str = ""  # Tetragon's, else the parent object's own exec_id
    # The event's mount-namespace number, as the kernel reported it: what the join matched
    # against the snapshots — so a consumer can group or re-join events itself (additive).
    mnt: int | None = None
    crossed: bool = False  # additive: see the class doc


def _kind(body: dict, kind: str) -> tuple[str, str]:
    if kind == "process_exec":
        return "exec", ""
    if kind == "process_exit":
        return "exit", str(body.get("status", "") or body.get("signal", "") or "")
    if kind == "process_kprobe":
        fn = body.get("function_name", "")
        arg = (body.get("args") or [{}])[0]
        if fn == "tcp_connect":
            s = arg.get("sock_arg", {})
            return "connect", f"{s.get('daddr', '?')}:{s.get('dport', '?')}"
        if "file_arg" in arg:
            args = body.get("args") or []
            mask = args[1].get("int_arg") if len(args) > 1 else None
            what = {2: "write", 4: "read"}.get(mask or 0, f"mask {mask}")
            ret = (body.get("return") or {}).get("int_arg")
            outcome = (
                "" if ret in (0, None) else f" → {ret}"
            )  # a refused access, as the kernel said
            return "file", f"{what} {arg['file_arg'].get('path', '?')}{outcome}"
        return "other", fn
    return "other", kind


def worktree_of(meta: dict | None, prefix: str) -> str:
    """The worktree a container CLAIMS to belong to: its compose project (``<prefix>`` = main,
    ``<prefix>-<wt>``) or the box name foldyard gives (``<project>-devbox``)."""
    if not meta:
        return ""
    project = meta.get("project") or ""
    name = meta.get("name") or ""
    if not project and name.endswith("-devbox"):
        project = name[: -len("-devbox")]
    if project == prefix:
        return "main"
    if project.startswith(prefix + "-"):
        return project[len(prefix) + 1 :]
    return ""


_ns = monitorstore.stamp_ns


def _handover(
    before: list[str],
    after: list[str],
    marks: list[tuple[int, str, int | None]],
    seq: int,
    started: int | None,
    taken: int | None,
) -> str:
    """The container an event belongs to when the snapshot before names ``before`` for its
    namespace and the snapshot after names ``after`` — the number changed hands between them — or
    ``""`` when the stream can't say. ``marks`` are that namespace's container-init marks between
    the two snapshots (:func:`monitorstore.init_mark`), as ``(seq, "start"|"end", process start)``
    in nanoseconds; ``taken`` is when the snapshot before was taken, ``started`` when the event's
    process started.

    * From the LAST init start on, when no init exit follows it, the namespace is the one the
      snapshot after names: nothing else entered it as a container since. Only an init that
      started after the snapshot before was taken: the relay reads Tetragon's export a moment
      behind, so that snapshot may already have seen this very init (it then names the same
      container, or that container came and went). And only for a process that started no
      earlier than that init — Tetragon's per-CPU buffers can deliver an event of the old
      container a moment late, and its process started before the old container died.
    * Up to the FIRST init exit, when no init start comes before it, it is the one the snapshot
      before names: that container's init was still running — an init that started before that
      snapshot was taken, so the one it saw.

    Between the two — the old container's children dying after its init, podman's own short-lived
    namespace (netavark) holding the number — neither, and a container that came and went inside
    the window (a start and its exit) blocks both. The marks only ever choose between the two
    containers the KERNEL snapshots named; they never name one themselves."""
    if len(before) != 1 or len(after) != 1:
        return ""
    starts = [m for m in marks if m[1] == "start"]
    ends = [m for m in marks if m[1] == "end"]
    if taken is None:
        return ""
    if starts:
        last = starts[-1]
        if (
            not any(e[0] > last[0] for e in ends)
            and seq >= last[0]
            and last[2] is not None
            and last[2] > taken
            and started is not None
            and started >= last[2]
        ):
            return after[0]
    if ends:
        first = ends[0]
        if (
            not any(s[0] < first[0] for s in starts)
            and seq <= first[0]
            and first[2] is not None
            and first[2] < taken
        ):
            return before[0]
    return ""


_Facts = monitorstore.NsFacts


def _lifetime(
    facts: _Facts,
    inode: str,
    seq: int,
    window: tuple[int, int],
    before: bool,
    again: bool,
    proc: tuple[str, bool, int | None],
) -> bool:
    """Whether an event the snapshots give to one container falls within that container's life —
    from its init entering the namespace to its init leaving it, as the init's marks say (a life is
    paired by its init's pid namespace) — or, where no mark says, is taken as the snapshots do.
    ``window``: the snapshot before and the one after (seqs); ``before``: the snapshot before
    names the container (else only the one after does); ``again``: the one after names it too;
    ``proc``: the event's process's pid namespace, whether that is the VM's (where podman's tools
    run) and when the process started.

    * The life holding the number at the event is the newest container init mark at or before it.
      None: the snapshots decide — except, named only by the snapshot after, before an init
      entering later in the window (podman's netavark, nft and crun run for an instant in a
      namespace of their own carrying the number the container takes next).
    * A life in progress is the container's when the snapshot before saw it (it started before
      that snapshot, or after it with no init leaving the number in between: the snapshot named the
      container while the runtime set it up — cabash's relay did, 31 ms before an init's exec), or,
      named only after, when it still runs at the snapshot after; another life started in between
      is it again only if the snapshot after names it too (a restart keeps the id) — and never for
      a process that started before the life's init (an event of the one before it, delivered
      late).
    * After a life's init left: the container's own processes, killed after their init, are in its
      pid namespace and still its — when that life was the container's; nothing else is (podman's
      teardown on the freed number, or the next container's processes) — except, where the number
      was free since before the window, the snapshots' container's own, if Tetragon never marked it,
      never podman's tools.

    The marks are init marks from podman's namespace only (:class:`_Facts`): a container can make
    an init of its own, but it never comes from there. A snapshot taken while the runtime was
    still setting the container up (its init's exec after it in the stream) names it for podman's
    tools just before, too: 157 events over cabash's whole store (2026-10-23). Telling those apart
    needs the next window's marks, which a hook — taking an event as settled at the first snapshot
    after it — can't wait for."""
    lo, hi = window
    pidns, host_pid, started = proc
    marks = facts.inits(inode, lo, hi)
    if not before and any(w == "start" and seq < q for q, w, _, _ in marks):
        return False
    h = facts.holder(inode, seq)
    if h is None or h[1] != "container":
        return True
    q, _, what, t0, life = h

    def seen_by_before(start: tuple | None) -> bool:  # that life is the one the snapshot before saw
        # it started before that snapshot, or after it with no init leaving the number in between:
        # the snapshot saw its namespace while the runtime set it up (the relay reads the export a
        # moment behind, and a container's scope holds the runtime before its init's exec)
        return (
            start is None
            or start[0] <= lo
            or not any(w == "end" and lo < m < start[0] for m, w, _, _ in marks)
        )

    if what == "start":
        if started is not None and t0 is not None and started < t0:
            return False
        ended = any(w == "end" and p == life and q < m for m, w, _, p in marks)
        if before and seen_by_before(h):
            return True
        return again and not ended
    if pidns and pidns == life:  # the ended life's own processes, dying
        return before and seen_by_before(facts.start_of(inode, life, q))
    return q <= lo and not host_pid


def _fallback(facts: _Facts, inode: str, seq: int, host_pid: bool) -> str:
    """``who`` for an event no container's life holds, by the newest fact about its namespace."""
    h = facts.holder(inode, seq)
    if h is None:
        return "unattributed"
    if h[1] == "entry":
        return "podman" if facts.podman(inode, seq) else "unattributed"
    if h[1] == "build" and (h[2] == "start" or not host_pid):
        return "build"
    return "unattributed"


def _taken(snap: dict) -> int | None:
    """When a snapshot was taken, in nanoseconds (the relay's clock: the guest's, as Tetragon's)."""
    t = snap.get("t")
    return round(t * 10**9) if isinstance(t, int | float) else None


def _is_host(obj: dict, kind: str) -> bool:
    return ((obj.get("ns") or {}).get(kind) or {}).get("is_host") is True


def _mnt(obj: dict) -> str:
    return str(((obj.get("ns") or {}).get("mnt") or {}).get("inum") or "")


def _one(
    known: _Facts, around: tuple[dict | None, dict | None], seq: int, proc: dict, parent: dict
) -> tuple[str, str, bool]:
    """``(who, container, crossed)`` for one event (:func:`attribute`): its process ``proc`` and
    that process's ``parent``, between the snapshots ``around`` it (``None`` past either end)."""
    snap_b, snap_a = around
    inode, pmnt = _mnt(proc), _mnt(parent)
    vm = {str(s.get("vm")) for s in around if s}
    lo = int(snap_b["seq"]) if snap_b else -1
    hi = int(snap_a["seq"]) if snap_a else 1 << 62

    def named(i: str) -> tuple[list[str] | None, list[str] | None]:
        return (snap_b["map"].get(i) if snap_b else None, snap_a["map"].get(i) if snap_a else None)

    def system(i: str, obj: dict) -> bool:  # the VM's own namespace, or one podman entered
        return i in vm or _is_host(obj, "mnt") or _is_host(obj, "user") or known.entered(i, seq)

    host_pid = _is_host(proc, "pid")
    before, after = named(inode)
    if system(inode, proc) and pmnt and pmnt != inode and not system(pmnt, parent):
        pb, pa = named(pmnt)  # crossed: named after where it came from
        ids = pb or pa
        if ids and len(ids) == 1 and not (pb and pa and pb != pa):
            return "container", ids[0], True
        who = _fallback(known, pmnt, seq, host_pid)
        return ("unattributed" if who == "podman" else who), "", True
    if inode in vm or _is_host(proc, "mnt") or _is_host(proc, "user"):
        return ("ambiguous" if before or after else "vm"), "", False
    if before and after and before != after:
        cid = _handover(
            before,
            after,
            [m[:3] for m in known.inits(inode, lo, hi)],
            seq,
            _ns(proc.get("start_time")),
            _taken(snap_b) if snap_b else None,
        )
        if cid:
            return "container", cid, False
        # neither container's: podman's own or a build's, when the facts say so
        fallback = _fallback(known, inode, seq, host_pid)
        return (fallback if fallback in ("podman", "build") else "ambiguous"), "", False
    ids = before or after
    if ids and len(ids) == 1:
        if known.podman(inode, seq):
            return "ambiguous", "", False  # a container's scope in podman's namespace
        pidns = str(((proc.get("ns") or {}).get("pid") or {}).get("inum") or "")
        window = (lo, hi)
        started = _ns(proc.get("start_time"))
        if _lifetime(
            known, inode, seq, window, bool(before), after == ids, (pidns, host_pid, started)
        ):
            return "container", ids[0], False
    elif ids:
        return "ambiguous", "", False
    return _fallback(known, inode, seq, host_pid), "", False


def attribute(records: Iterable[dict], prefix: str) -> list[Event]:
    """Join events to containers, per boot, over the stored records (in store order).

    For each event, look at the LAST snapshot before it and the FIRST snapshot after it, and use
    only one that contains the event's mount namespace:

    * the snapshot before, when it still holds the namespace — the container was alive then;
    * otherwise the snapshot after — the relay snapshots within a second of an unseen namespace,
      so a container that started just before its first event is named by the next one;
    * both, disagreeing: the number was handed from one container to the next between them (the
      kernel gives a freed number to the next namespace made). The containers' own inits say where
      — the old one's leaving, the new one's entering (:func:`_handover`) — and where they can't,
      ``ambiguous``. A mapping is otherwise only ever trusted from the snapshot nearest the event,
      never carried forward past a snapshot that no longer had it.

    A container's name holds only within its life (:func:`_lifetime`). Every namespace no
    container holds is told by kernel facts (:class:`_Facts`, :class:`Event`): the VM's own (as
    those two snapshots report it, or Tetragon's ``is_host`` on the mount or user namespace) is
    ``vm``; the one podman enters from it is ``podman``; an image build step's is ``build``;
    anything else ``unattributed``. A container's scope holding a process in the VM's or podman's
    namespace is ``ambiguous``; a process in one of those whose parent was in a namespace that
    isn't is ``crossed``, and named after its parent's. Duplicate (boot, seq) records — a re-read
    after a crash — count once. Only the two snapshots around an event and the facts before it
    decide it, so joining a stretch of the store with the snapshots and facts the catalogue holds
    around it gives what joining the whole store would (:func:`join`); ``{"k": "fact"}`` records
    (the catalogue's) are read as facts only, never as events.
    """
    by_boot: dict[str, list[dict]] = {}
    facts: dict[str, dict[int, dict]] = {}
    meta: dict[str, dict | None] = {}
    seen: set[tuple[str, int]] = set()
    for r in records:
        k = r.get("k")
        if k == "engine":
            meta.update(r.get("containers") or {})
            continue
        if "boot" not in r:
            continue
        if k == "fact":
            facts.setdefault(r["boot"], {})[int(r["seq"])] = r
            continue
        if k in ("ev", "scopes"):
            key = (r["boot"], int(r["seq"]))
            if key in seen:
                continue
            seen.add(key)
            by_boot.setdefault(r["boot"], []).append(r)
            f = monitorstore.ns_fact(r)
            if f is not None:
                facts.setdefault(r["boot"], {})[int(r["seq"])] = {**f, "seq": int(r["seq"])}

    out: list[Event] = []
    for boot, recs in by_boot.items():
        recs.sort(key=lambda r: int(r["seq"]))
        snaps = [r for r in recs if r["k"] == "scopes"]
        known = _Facts(facts.get(boot, {}).values())
        si = 0  # index of the first snapshot AFTER the current record
        for r in recs:
            if r["k"] == "scopes":
                si += 1
                continue
            seq = int(r["seq"])
            ev = r.get("e") or {}
            kind = next((k for k in ev if k.startswith("process_")), "")
            body = ev.get(kind) or {}
            proc = body.get("process") or {}
            parent = body.get("parent") or {}
            inode = _mnt(proc)
            around = (snaps[si - 1] if si > 0 else None, snaps[si] if si < len(snaps) else None)
            who, cid, crossed = _one(known, around, seq, proc, parent)
            what, detail = _kind(body, kind)
            m = meta.get(cid) if cid else None
            out.append(
                Event(
                    time=ev.get("time", ""),
                    boot=boot,
                    seq=seq,
                    kind=what,
                    binary=proc.get("binary", ""),
                    args=proc.get("arguments", ""),
                    detail=detail,
                    uid=proc.get("uid"),
                    pid=proc.get("pid"),
                    who=who,
                    container=cid,
                    name=(m or {}).get("name", "") if m else "",
                    worktree=worktree_of(m, prefix),
                    ppid=parent.get("pid"),
                    parent=parent.get("binary") or "",
                    parent_args=parent.get("arguments") or "",
                    exec_id=proc.get("exec_id") or "",
                    parent_exec_id=proc.get("parent_exec_id") or parent.get("exec_id") or "",
                    mnt=int(inode) if inode.isdigit() else None,
                    crossed=crossed,
                )
            )
    return out


READ_BYTES = 8 << 20  # how much of the store's tail `recent` reads
CHUNK = 5_000  # records joined at a time when reading a range (tens of MB parsed, at most)


def join(records: list[dict], cat: monitorstore.Catalogue | None, prefix: str) -> list[Event]:
    """:func:`attribute` over ``records`` — one contiguous stretch of the store — giving what
    joining the WHOLE store would: with the catalogue's snapshot before and after each boot's
    events in it, and every container's latest engine claim (put last, so it is the last word,
    as in the store). Without a catalogue, over ``records`` alone."""
    if cat is None:
        return attribute(records, prefix)
    span: dict[str, tuple[int, int]] = {}
    for r in records:
        if r.get("k") == "ev" and "boot" in r:
            seq = int(r["seq"])
            lo, hi = span.get(r["boot"], (seq, seq))
            span[r["boot"]] = (min(lo, seq), max(hi, seq))
    near = []
    for boot, (lo, hi) in span.items():
        before, after = cat.before(boot, lo), cat.after(boot, hi)
        near += [s for s in (before, after) if s]
        # the facts from the snapshot before to the one after (a container's init entering its
        # namespace may sit in an earlier read than its first command)…
        blo = before["seq"] if before else -1
        window = cat.facts(boot, blo, after["seq"] if after else 1 << 62)
        # …and, before that, what the join asks about every namespace the events touch: each fact
        # carries its kind, so the newest few of some kinds (Catalogue.prior: podman entered its
        # own at boot; a container's init may have entered hours ago) — what retention keeps
        inodes = {str(f["inode"]) for f in window} | {str(f["outside"]) for f in window}
        for r in records:
            if r.get("k") == "ev" and r.get("boot") == boot:
                body = next((v for k, v in (r.get("e") or {}).items() if k.startswith("process_")),
                            {}) or {}  # fmt: skip
                for obj in (body.get("process") or {}, body.get("parent") or {}):
                    inum = ((obj.get("ns") or {}).get("mnt") or {}).get("inum")
                    if inum:
                        inodes.add(str(inum))
        prior = [f for i in sorted(inodes) for f in cat.prior(boot, i, blo)]
        near += prior + window
    # the snapshots after the records: attribute() orders boots by first appearance, and the
    # events must come out in store order (a hook's cursor moves by them)
    return attribute([*records, *near, {"k": "engine", "containers": cat.claims()}], prefix)


def catalogue(segs: list[monitorstore.Segment] | None = None) -> monitorstore.Catalogue | None:
    """The store's catalogue, brought up to date with ``segs`` — ``None`` when it can't be opened
    (the join then sees only what it reads, and says less)."""
    segs = monitorstore.segments() if segs is None else segs
    cat = monitorstore.Catalogue.open_or_none() if segs else None  # no store, nothing to make
    if cat is None:
        return None
    try:
        cat.catch_up(segs, whole=True)  # every caller passes a full listing
    except Exception:
        cat.close()
        return None
    return cat


def recent(limit: int = 50, prefix: str | None = None) -> list[Event]:
    """The last ``limit`` attributed events, from the tail of the store."""
    segs = monitorstore.segments()
    read, _ = monitorstore.read(monitorstore.tail(READ_BYTES, segs), READ_BYTES, segs)
    cat = catalogue(segs)
    try:
        prefix = prefix if prefix is not None else config.project_prefix()
        events = join([r for r, _ in read], cat, prefix)
    finally:
        if cat is not None:
            cat.close()
    return events[-limit:]


def when(text: str, now: float | None = None) -> str:
    """A ``--since``/``--until`` value as an event-time bound: an ISO time (UTC unless it says
    otherwise, like the times ``fy monitor`` prints) or a span back from now — ``90s``, ``15m``,
    ``2h``, ``1d``. :class:`ValueError` for anything else."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd])\s*", text)
    if m:
        try:
            back = timedelta(seconds=float(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]])
            t = datetime.fromtimestamp(time.time() if now is None else now, UTC) - back
        except OverflowError:
            raise ValueError(f"{text}: further back than a date can go") from None
    else:
        t = datetime.fromisoformat(text.strip())
        t = (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC)
    # nine fractional digits, as Tetragon writes them, so the bound compares as text
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _gap_note(r: dict) -> str:
    seen = _iso(float(r.get("recv", 0.0)))
    if "from" in r:
        n = int(r["to"]) - int(r["from"]) + 1
        return (
            f"{n} events lost between the VM and here (boot {str(r.get('boot'))[:8]}, seen {seen})"
        )
    return f"the relay restarted and missed Tetragon's events up to {seen}: {r.get('why', '')}"


def between(since: str, until: str, prefix: str, notes: list[str]) -> Iterator[Event]:
    """Every stored event with ``since <= time < until`` (bounds from :func:`when`; "" = open),
    oldest first, joined as over the whole store — read segment by segment (each one's time
    bounds come from the catalogue, so a range skips what it can't touch), never all at once.
    ``notes`` collects what is missing in the range: transport and relay gaps, and a range
    reaching back past the store's oldest record."""
    segs = monitorstore.segments()
    cat = catalogue(segs)
    try:
        bounds = cat.bounds() if cat is not None else {}
        oldest = bounds.get(segs[0].id, ("", ""))[0] if segs else ""
        if since and oldest and oldest > since:
            notes.append(
                f"the store reaches back to {oldest[:19].replace('T', ' ')} UTC — older events "
                "were removed to keep it within its budget (`max_mb` in "
                f"{monitorstore.settings_file()}; `fy docs configuration`)"
            )

        def touches(seg: monitorstore.Segment) -> bool:
            t0, t1 = bounds.get(seg.id, ("", ""))
            return seg.live or not t0 or ((not since or t1 >= since) and (not until or t0 < until))

        def inside(e: Event) -> bool:
            return bool(e.time) and (not since or e.time >= since) and (not until or e.time < until)

        for seg in (s for s in segs if touches(s)):
            buf: list[dict] = []
            for rec, _at in monitorstore.scan([seg.id, 0], [seg]):
                if rec.get("k") == "gap":
                    t = datetime.fromtimestamp(float(rec.get("recv", 0.0)), UTC)
                    at = t.strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"
                    if (not since or at >= since) and (not until or at < until):
                        notes.append(_gap_note(rec))
                buf.append(rec)
                if len(buf) >= CHUNK:
                    yield from filter(inside, join(buf, cat, prefix))
                    buf = []
            yield from filter(inside, join(buf, cat, prefix))
    finally:
        if cat is not None:
            cat.close()


# ── `fy monitor` ──────────────────────────────────────────────────────────────────────


def _who(e: Event) -> str:
    label = e.who
    if e.who == "container":
        label = f"{e.worktree or '?'}:{e.name or e.container[:12]}"
    return f"⚠ crossed {label}" if e.crossed else label


def store_summary() -> str:
    """How much the store holds on your computer, against its budget, and how far back."""
    segs = monitorstore.segments()
    used = sum(monitorstore.disk_size(s) for s in segs)
    cap, note = monitorstore.budget_setting()
    cat = monitorstore.Catalogue.open_or_none() if segs else None
    oldest = ""
    if cat is not None:
        try:
            oldest = cat.bounds().get(segs[0].id, ("", ""))[0]
        except Exception:
            oldest = ""
        finally:
            cat.close()
    since = f", events from {oldest[:19].replace('T', ' ')} UTC" if oldest else ""
    warn = f" ⚠ {note}" if note else ""
    return f"store: {used / 2**20:.0f} of {cap >> 20} MB{since}{warn}"


def show(
    limit: int | None = 40,
    worktree: str = "",
    as_json: bool = False,
    since: str = "",
    until: str = "",
) -> int:
    """`fy monitor`: the last ``limit`` events the guest monitor recorded, attributed — or, with
    ``since``/``until``, every event in that range (the last ``limit`` of them when given), read
    from the whole store. Host only: the store lives on the host, outside the box's reach."""
    if config.in_box():
        print("✗ run on your computer — the monitor's events are kept there, out of the box")
        return 1
    ranged = bool(since or until)
    try:
        lo, hi = (when(since) if since else ""), (when(until) if until else "")
    except ValueError:
        print(
            "✗ --since/--until take an ISO time (UTC unless it gives an offset, e.g. "
            "2026-10-21T19:55) or a span back from now: 90s, 15m, 2h, 1d"
        )
        return 1
    notes: list[str] = []
    events: Iterable[Event] = (
        between(lo, hi, config.project_prefix(), notes) if ranged else recent(limit=100_000)
    )
    if worktree:
        events = (e for e in events if e.worktree == worktree)
    if limit is None and not ranged:
        limit = 40
    if limit is not None:
        events = deque(events, maxlen=limit)
    shown = 0
    for e in events:
        shown += 1
        if as_json:
            print(json.dumps(asdict(e)))
        else:
            what = e.detail or e.args
            stamp = e.time[:19].replace("T", " ") if ranged else e.time[11:19]
            print(f"{stamp}  {_who(e):<28.28}  {e.kind:<7}  {e.binary:<28.28}  {what[:80]}")
    if as_json:
        for note in notes:
            print(f"⚠ {note}", file=sys.stderr)
        return 0
    if not shown:
        where = f" for worktree '{worktree}'" if worktree else ""
        print(("no monitor events in that range" if ranged else "no monitor events yet") + where)
    for note in notes:
        print(f"⚠ {note}")
    cur = Cursor.load()
    age = f"{time.time() - cur.pulled:.0f}s ago" if cur.pulled else "never"
    print(
        f"— last pull {age}; {cur.stored} stored, {cur.gaps} lost in transit, {cur.forged} "
        "failed their signature. Names and worktrees are what the engine reports (claims); "
        "`build` is an image build's step, `podman` and `vm` the engine's and the VM's own, "
        "`unattributed` none the kernel's records can name (podman's tools for an instant, a "
        "container too short-lived to be seen); `⚠ crossed` came out of a container into the "
        "VM's or podman's namespace. Times are UTC."
    )
    print(f"— {store_summary()}")
    if cur.error:
        print(f"⚠ the last pull failed: {cur.error}")
    return 0
