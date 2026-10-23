"""monitorstore.py — the guest monitor's event store on your computer (docs/monitor-pipeline.md
§2–§3): the segments the pull appends to, their compression and retention, the catalogue that lets
a reader join any stretch of them, and reading by position.

**Segments.** ``<state dir>/logs/monitor.jsonl`` is the live one, appended to by the pull (the only
writer). Its first line names it — ``{"k": "segment", "id": …}`` — and the id stays with it: past
:data:`SEGMENT_BYTES` it is renamed ``monitor.<id>.jsonl`` and then compressed to
``monitor.<id>.jsonl.gz`` (gzip, ~30× on these records: 2.4 KB raw → ~80 B per event, measured on a
real store). A position in the store is ``[segment id, offset]``, the offset counted in the
segment's UNCOMPRESSED bytes, so it survives both the rename and the compression — the reason a
position is no longer an inode (one that the compressed file doesn't keep, and ext4 hands out
again). A live file without that first line was written by an older foldyard: the next append
seals it under a fresh id, and anything that held a position in it finds its place again by the
event's ``(boot, seq)``.

**Retention** is a disk budget, not a count of files: the oldest sealed segments go once the
segments together pass :func:`budget`. The budget is host-only — ``max_mb`` in
``<state dir>/monitor-store.toml``, never ``foldyard.toml``: the store is the evidence of what ran
in the box, so the box mustn't be able to shorten it (nor fill your computer's disk with it).

**The catalogue** (``logs/monitor-index.sqlite``, derived data: deleted, it is rebuilt from the
segments) holds every snapshot and the latest engine claim per container, so a reader joining ANY
stretch of the store sees the snapshot before it and the one after it wherever they are, and the
claim naming a container however long ago it was made. The pull writes a record's catalogue entry
BEFORE the record itself — a crash between the two re-stores the record (the pull's cursor moves
only after), and an entry seen twice counts once. Every reader first indexes what isn't yet
(:meth:`Catalogue.catch_up`), so an upgrade or a lost catalogue costs one scan, not wrong answers.
When a segment leaves the store its snapshots go too, except the newest one of each boot: the
snapshot before the oldest events still kept.

It also keeps every container init's MARK — its exec into its mount namespace and its exit from it
(:func:`init_mark`) — for the same reason it keeps the snapshots: the join reads a namespace number
handed from one container to the next by them (:func:`foldyard.monitorlog.attribute`), and the
mark may sit in an earlier read than the event it decides. Beside them it keeps each namespace's
FACTS (:func:`ns_fact`): the same marks with where the init came from (its parent's namespace and
binary), and podman entering a namespace straight from the VM's own — how the join tells podman's
own namespace, an image build and a container apart, however long ago the fact was recorded.
"""

from __future__ import annotations

import gzip
import json
import os
import posixpath
import re
import shutil
import sqlite3
import time
import tomllib
import zlib
from bisect import bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BufferedReader
from pathlib import Path

from . import config

SEGMENT_BYTES = 32 << 20  # the live segment is sealed past this (uncompressed)
BUDGET_MB = 8192  # default disk budget for the whole store (per project)
BUDGET_MIN_MB, BUDGET_MAX_MB = 64, 65536
LEGACY = "legacy"  # an older foldyard's live segment (no first line naming it) is "legacy-<inode>"
_CLAIMS_KEPT = 10_000  # engine claims, newest first (containers ever described)
_ORPHANS_KEPT = 64  # snapshots kept from segments already gone (the newest per boot)
_SCHEMA = 2
# The marks (:func:`init_mark`) live in tables of their own BESIDE schema 2, never in it: an
# editable install upgrades the CLI at once and the supervisor only at its restart, and 0.6's
# catalogue code drops and rebuilds a catalogue whose schema it doesn't know (losing the snapshots
# kept from removed segments) and fails every write to a `seg` with a column it doesn't know.
# `markseg.marked` is how far a segment has been read for marks: behind `seg.indexed` wherever 0.6
# indexed (:meth:`Catalogue.catch_up` reads the marks out of that part first).
_MARK_SQL = (
    "CREATE TABLE IF NOT EXISTS mark (boot TEXT NOT NULL, seq INTEGER NOT NULL, seg TEXT NOT NULL, "
    "inode TEXT NOT NULL, what TEXT NOT NULL, start TEXT NOT NULL, PRIMARY KEY (boot, seq))",
    "CREATE TABLE IF NOT EXISTS markseg (id TEXT PRIMARY KEY, marked INTEGER NOT NULL)",
)
# The facts (:func:`ns_fact`) go beside schema 2 the same way, with their own progress
# (`nsfactseg`): a supervisor that knows only the marks indexes without them, and the next reader
# reads them out of what it indexed (:meth:`Catalogue.catch_up`).
_FACT_SQL = (
    "CREATE TABLE IF NOT EXISTS nsfact (boot TEXT NOT NULL, seq INTEGER NOT NULL, "
    "seg TEXT NOT NULL, inode TEXT NOT NULL, what TEXT NOT NULL, start TEXT NOT NULL, "
    "outside TEXT NOT NULL, by TEXT NOT NULL, pidns TEXT NOT NULL, kind TEXT NOT NULL, "
    "taken INTEGER NOT NULL, PRIMARY KEY (boot, seq))",
    "CREATE INDEX IF NOT EXISTS nsfact_inode ON nsfact (boot, inode, what, seq)",
    "CREATE INDEX IF NOT EXISTS nsfact_seq ON nsfact (boot, inode, seq)",
    "CREATE TABLE IF NOT EXISTS nsfactseg (id TEXT PRIMARY KEY, done INTEGER NOT NULL)",
)
_SCHEMA_SQL = (
    "DROP TABLE IF EXISTS seg",
    "DROP TABLE IF EXISTS snap",
    "DROP TABLE IF EXISTS claim",
    "DROP TABLE IF EXISTS span",
    "DROP TABLE IF EXISTS mark",
    "DROP TABLE IF EXISTS markseg",
    "DROP TABLE IF EXISTS nsfact",
    "DROP TABLE IF EXISTS nsfactseg",
    "CREATE TABLE seg (id TEXT PRIMARY KEY, indexed INTEGER NOT NULL, sealed INTEGER NOT NULL, "
    "t0 TEXT NOT NULL, t1 TEXT NOT NULL)",
    "CREATE TABLE snap (boot TEXT NOT NULL, seq INTEGER NOT NULL, seg TEXT NOT NULL, "
    "rec TEXT NOT NULL, PRIMARY KEY (boot, seq))",
    "CREATE TABLE claim (cid TEXT PRIMARY KEY, meta TEXT NOT NULL, n INTEGER NOT NULL)",
    "CREATE TABLE span (seg TEXT NOT NULL, boot TEXT NOT NULL, lo INTEGER NOT NULL, "
    "hi INTEGER NOT NULL, PRIMARY KEY (seg, boot))",
    *_MARK_SQL,
    *_FACT_SQL,
)
# every mark's line holds one (the store writes compact JSON; the spaced form, should anything
# else have): the backfill parses only those — `"in_init_tree"` alone is in nearly every line
_MARK_NEEDLES = (b'"in_init_tree":true', b'"in_init_tree": true')
# …and every fact's line holds one of those, or a mount namespace that is the VM's and one that
# isn't (an entry: the parent in the VM's, the process not) — about 1% of cabash's lines
_HOST_MNT = re.compile(rb'"mnt":\s*\{"inum":\s*\d+,\s*"is_host":\s*true\}')
_OTHER_MNT = re.compile(rb'"mnt":\s*\{"inum":\s*(\d+)\s*\}')


_RUNS = ("entry", "other")  # kinds whose run (same kind, same taken) is kept as its first


def _may_hold_fact(line: bytes) -> bool:
    if any(n in line for n in _MARK_NEEDLES):
        return True
    others = set(_OTHER_MNT.findall(line))
    # an entry (the VM's and one that isn't), or two that aren't the VM's (`other`)
    return bool(others) and (len(others) > 1 or bool(_HOST_MNT.search(line)))


def store_dir() -> Path:
    return config.state_dir() / "logs"


def live_path() -> Path:
    return store_dir() / "monitor.jsonl"


def index_path() -> Path:
    return store_dir() / "monitor-index.sqlite"


def settings_file() -> Path:
    return config.state_dir() / "monitor-store.toml"


# ── the budget ────────────────────────────────────────────────────────────────────────


def budget_setting() -> tuple[int, str]:
    """``(bytes, note)``: the store's disk budget from the host-only settings file, clamped to
    :data:`BUDGET_MIN_MB`–:data:`BUDGET_MAX_MB`; ``note`` says why the file wasn't taken as
    written ("" when it was, or there is none). Re-read on every append, so an edit applies
    within seconds."""
    try:
        doc = tomllib.loads(settings_file().read_text())
    except FileNotFoundError:
        return BUDGET_MB << 20, ""
    except (OSError, tomllib.TOMLDecodeError) as e:
        return BUDGET_MB << 20, f"{settings_file()} unreadable ({e}): using {BUDGET_MB} MB"
    raw = doc.get("max_mb", BUDGET_MB)
    if not isinstance(raw, int) or isinstance(raw, bool):
        return BUDGET_MB << 20, f"`max_mb` must be a whole number of MB: using {BUDGET_MB} MB"
    mb = min(max(raw, BUDGET_MIN_MB), BUDGET_MAX_MB)
    note = "" if mb == raw else f"`max_mb = {raw}` is outside {BUDGET_MIN_MB}–{BUDGET_MAX_MB}"
    return mb << 20, note + (f": using {mb} MB" if note else "")


def budget() -> int:
    return budget_setting()[0]


# ── segments ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Segment:
    id: str
    path: Path  # where it was when listed; :func:`_open` follows a rename or a compression
    live: bool = False

    @property
    def gz(self) -> bool:
        return self.path.suffix == ".gz"


def _sealed_id(name: str) -> str | None:
    if name == "monitor.jsonl" or not name.startswith("monitor."):
        return None
    for suffix in (".jsonl.gz", ".jsonl"):
        if name.endswith(suffix):
            sid = name[len("monitor.") : -len(suffix)]
            return sid if sid and "." not in sid else None
    return None


def _header(path: Path) -> str | None:
    """The id a live segment's first line gives it, ``None`` when there is no such file. One an
    older foldyard wrote has no such line: it is ``legacy-<inode>`` — stable while that foldyard
    keeps appending to it, and a different id once it rotates it and starts another (which would
    otherwise inherit the first one's place in the catalogue)."""
    try:
        with path.open("rb") as f:
            first = f.readline(4096)
            ino = os.fstat(f.fileno()).st_ino
    except OSError:
        return None
    try:
        doc = json.loads(first)
    except ValueError:
        doc = None
    if isinstance(doc, dict) and doc.get("k") == "segment" and isinstance(doc.get("id"), str):
        return doc["id"]
    return f"{LEGACY}-{ino}"


def segments() -> list[Segment]:
    """The store's segments, oldest first, the live one last. A sealed segment present both raw
    and compressed (a compression interrupted after its rename) is the compressed one."""
    try:
        names = os.listdir(store_dir())
    except OSError:
        names = []
    sealed: dict[str, Path] = {}
    for name in names:
        sid = _sealed_id(name)
        if sid is not None and (sid not in sealed or name.endswith(".gz")):
            sealed[sid] = store_dir() / name
    out = [Segment(sid, sealed[sid]) for sid in sorted(sealed)]
    live = _header(live_path())
    if live is not None and live not in sealed:
        out.append(Segment(live, live_path(), live=True))
    return out


def _open(seg: Segment) -> BufferedReader | gzip.GzipFile | None:
    """``seg``'s bytes, uncompressed — wherever it is now: a live segment may have been sealed,
    and a sealed one compressed, since it was listed."""
    if seg.live and _header(live_path()) == seg.id:
        try:
            return live_path().open("rb")
        except OSError:
            pass
    base = store_dir() / f"monitor.{seg.id}.jsonl"
    try:
        return base.open("rb")
    except OSError:
        pass
    try:
        f = gzip.open(base.with_name(base.name + ".gz"), "rb")
        f.peek(1)  # a missing or unreadable file fails here, not mid-read
        return f
    except (OSError, EOFError, zlib.error):
        return None


def raw_size(seg: Segment) -> int:
    """``seg``'s uncompressed size (gzip records it, mod 2³², in its last four bytes; a segment
    is sealed at tens of MB)."""
    try:
        if not seg.gz:
            return seg.path.stat().st_size
        with seg.path.open("rb") as f:
            f.seek(-4, os.SEEK_END)
            return int.from_bytes(f.read(4), "little")
    except OSError:
        return 0


def last_event_recv(within: int = 256 << 10) -> float:
    """When the store's newest event reached your computer (its ``recv``), from the live
    segment's last ``within`` bytes; 0 when there is none there."""
    try:
        with live_path().open("rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - within))
            lines = f.read().split(b"\n")
    except OSError:
        return 0.0
    for line in reversed(lines):
        if b'"k":"ev"' not in line:
            continue
        try:
            rec = json.loads(line)
            return float(rec.get("recv") or 0.0)
        except (ValueError, TypeError, AttributeError):
            continue
    return 0.0


def disk_size(seg: Segment) -> int:
    try:
        return seg.path.stat().st_size
    except OSError:
        return 0


def _lines(seg: Segment, off: int) -> Iterator[tuple[bytes, int]]:
    """``seg``'s complete lines from the uncompressed offset ``off`` on (a line ``off`` falls
    inside is skipped), each with the offset it ends at. A line still being written is left for
    the next read."""
    f = _open(seg)
    if f is None:
        return
    try:
        with f:
            if off > 0:
                f.seek(off - 1)
                if f.read(1) != b"\n":
                    rest = f.readline()
                    if not rest.endswith(b"\n"):
                        return
                    off += len(rest)
            for line in f:
                if not line.endswith(b"\n"):
                    return
                off += len(line)
                yield line, off
    except (OSError, EOFError, zlib.error):
        return  # a segment removed or cut under the reader: what was read stands


def scan(pos: list | None, segs: list[Segment] | None = None) -> Iterator[tuple[dict, list]]:
    """The stored records from ``pos`` (``[segment id, offset]``; ``None`` = the oldest) on, oldest
    first, each with the position its line ends at. A ``pos`` naming no segment yields nothing —
    :func:`locate` says whether it does."""
    segs = segments() if segs is None else segs
    i, off = (0, 0) if pos is None else (locate(pos, segs), int(pos[1]))
    if i is None:
        return
    for seg in segs[i:]:
        for line, end in _lines(seg, off):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("k") != "segment":
                yield rec, [seg.id, end]
        off = 0


def read(pos: list | None, max_bytes: int, segs: list[Segment] | None = None):
    """:func:`scan` from ``pos``, stopping once ``max_bytes`` of lines are read AND an event is
    among them (whole lines always; a reader's cursor moves by events, so a read of snapshots and
    claims alone would hold it where it is for good). Returns ``(records with positions,
    found)``, ``found`` False when ``pos`` names a segment the store no longer has."""
    segs = segments() if segs is None else segs
    if pos is not None:
        i = locate(pos, segs)
        # past its segment's end: that is another file now (the live one, sealed under a reader)
        if i is None or int(pos[1]) > raw_size(segs[i]):
            return [], False
    out: list[tuple[dict, list]] = []
    prev = None if pos is None else (pos[0], int(pos[1]))
    taken, event = 0, False
    for rec, at in scan(pos, segs):
        out.append((rec, at))
        taken += at[1] - prev[1] if prev is not None and prev[0] == at[0] else at[1]
        prev = (at[0], at[1])
        event = event or rec.get("k") == "ev"
        if taken >= max_bytes and event:
            break
    return out, True


def locate(pos: list, segs: list[Segment]) -> int | None:
    """The index in ``segs`` of the segment ``pos`` names, or ``None``. A position from an older
    foldyard names an inode (an int): never a segment here."""
    sid = pos[0] if pos else None
    if not isinstance(sid, str):
        return None
    return next((i for i, s in enumerate(segs) if s.id == sid), None)


def tail(max_bytes: int, segs: list[Segment] | None = None) -> list | None:
    """The position ``max_bytes`` before the store's end (``None`` for an empty store)."""
    segs = segments() if segs is None else segs
    left = max_bytes
    for seg in reversed(segs):
        size = raw_size(seg)
        if size >= left:
            return [seg.id, size - left]
        left -= size
    return [segs[0].id, 0] if segs else None


def find(key: list, segs: list[Segment] | None = None, cat: Catalogue | None = None) -> list | None:
    """The position just past the event ``key`` (``[boot, seq]``) — how a reader whose position
    was lost (an older foldyard's inode) finds its place again. ``None`` when the store no longer
    holds it. Only the segments the catalogue says hold that boot's seq are read — and every one
    it hasn't indexed yet, whose silence says nothing (a segment sealed since the catalogue last
    caught up: 0.6.1's upgrade took a hook's cursor for lost that way); without a catalogue, every
    segment, newest first. An event retention removed must not cost a scan of the whole store to
    find out."""
    segs = segments() if segs is None else segs
    boot, seq = str(key[0]), int(key[1])
    if cat is not None:
        holding = cat.holding(boot, seq) | cat.unindexed(segs)
        segs = [s for s in segs if s.id in holding]
    needle, also = f'"seq":{seq}'.encode(), json.dumps(boot).encode()
    for seg in reversed(segs):
        for line, end in _lines(seg, 0):
            if needle not in line or also not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("k") == "ev" and rec.get("boot") == boot and rec.get("seq") == seq:
                return [seg.id, end]
    return None


# ── appending: the pull's one write ───────────────────────────────────────────────────


def _new_id(segs: list[Segment]) -> str:
    """A new segment's id: its creation time, and always past every id the store has — segments
    are read in id order, so a clock stepped back must not file one before an older one."""
    sid = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    top = max((s.id for s in segs if not s.id.startswith(LEGACY)), default="")
    return sid if sid > top else top + "a"


def _dump(rec: dict) -> bytes:
    return (json.dumps(rec, separators=(",", ":")) + "\n").encode()


def append(records: list[dict]) -> None:
    """Store ``records`` (the pull's, in order): seal the live segment first when it is full (or
    an older foldyard's), start a new one named by its first line, index, then write. Then
    compress what was sealed and keep the store within :func:`budget`. The catalogue failing
    never stops the store: what it misses, the next reader indexes."""
    d = store_dir()
    d.mkdir(parents=True, exist_ok=True)
    segs = segments()
    live = segs[-1] if segs and segs[-1].live else None
    cat = Catalogue.open_or_none()
    try:
        if live is not None and (live.id.startswith(LEGACY) or raw_size(live) > SEGMENT_BYTES):
            if cat is not None:
                try:
                    # indexed BEFORE it is sealed — an older foldyard's live file was never: its
                    # rows then move with it to its new id, and no reader's catalogue is left
                    # without the segment a hook's cursor is in (cabash, 2026-10-22), nor does
                    # retention take its snapshots with it unseen
                    cat.catch_up([live])
                except sqlite3.Error:
                    pass
            _seal(live, segs, cat)
            live = None
        lines: list[bytes] = []
        if live is None:
            sid = _new_id(segs)
            lines.append(_dump({"k": "segment", "id": sid, "t": time.time()}))
            size = 0
        else:
            sid, size = live.id, raw_size(live)
        lines += [_dump(r) for r in records]
        if cat is not None:
            try:
                if live is not None:
                    cat.catch_up([live])  # anything a crash left unindexed, before it moves on
                cat.add(sid, records, indexed=size + sum(map(len, lines)), exact=True)
            except sqlite3.Error:
                pass
        with live_path().open("ab") as f:
            f.write(b"".join(lines))
        _compress_sealed()
        _prune(budget(), cat)
    finally:
        if cat is not None:
            cat.close()


def _seal(live: Segment, segs: list[Segment], cat: Catalogue | None) -> None:
    sid = live.id
    if sid.startswith(LEGACY):
        sid = _new_id(segs)
        if cat is not None:
            try:
                cat.rename(live.id, sid)
            except sqlite3.Error:
                pass
    live_path().replace(store_dir() / f"monitor.{sid}.jsonl")


def _compress_sealed() -> None:
    """Every sealed segment still raw → ``.jsonl.gz``, atomically (written aside, renamed, then
    the raw one removed: a reader holding it open reads on)."""
    for seg in segments():
        if seg.live or seg.gz:
            continue
        gz = seg.path.with_name(seg.path.name + ".gz")
        tmp = seg.path.with_name(f".{gz.name}.tmp")
        try:
            with seg.path.open("rb") as src, tmp.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
            tmp.replace(gz)
            seg.path.unlink()
        except OSError:
            tmp.unlink(missing_ok=True)


def _prune(limit: int, cat: Catalogue | None) -> None:
    """Remove the oldest sealed segments until the store fits ``limit`` bytes on disk. Never the
    live segment."""
    segs = segments()
    total = sum(disk_size(s) for s in segs)
    gone = []
    for seg in segs:
        if total <= limit or seg.live:
            break
        total -= disk_size(seg)
        seg.path.unlink(missing_ok=True)
        gone.append(seg.id)
    if gone and cat is not None:
        try:
            cat.drop(gone)
        except sqlite3.Error:
            pass


# ── the catalogue ─────────────────────────────────────────────────────────────────────


def _ev_time(rec: dict) -> str:
    return str((rec.get("e") or {}).get("time") or "") if rec.get("k") == "ev" else ""


def init_mark(rec: dict) -> dict | None:
    """``{"inode", "what", "start"}`` when ``rec`` is a container's init entering its mount
    namespace (``what`` = ``start``: the exec) or leaving it (``end``: the exit), else ``None``.

    A container's init is the one process that is in the init tree of the container's pid
    namespace (Tetragon's ``in_init_tree``, a kernel fact) AND whose parent is outside its mount
    namespace: podman's conmon starts it. ``podman exec`` also comes from outside, but never in the
    init tree; the init's own children are in it, but their parent is inside. ``start`` is the
    process's start time, as Tetragon wrote it."""
    if rec.get("k") != "ev":
        return None
    e = rec.get("e") or {}
    what = {"process_exec": "start", "process_exit": "end"}
    kind = next((k for k in e if k in what), None)
    if kind is None:
        return None
    body = e[kind] if isinstance(e[kind], dict) else {}
    proc, parent = body.get("process") or {}, body.get("parent") or {}
    if proc.get("in_init_tree") is not True:
        return None
    inode = ((proc.get("ns") or {}).get("mnt") or {}).get("inum")
    outside = ((parent.get("ns") or {}).get("mnt") or {}).get("inum")
    if not inode or not outside or outside == inode:
        return None
    return {"inode": str(inode), "what": what[kind], "start": str(proc.get("start_time") or "")}


def _ns(obj: dict, kind: str) -> dict:
    got = (obj.get("ns") or {}).get(kind)
    return got if isinstance(got, dict) else {}


def ns_fact(rec: dict) -> dict | None:
    """What ``rec`` says about who holds a mount namespace — ``{"inode", "what", "start",
    "outside", "by", "pidns"}`` — else ``None``. Kernel facts as Tetragon reports them, never a
    name:

    * ``start``/``end``: an init entering or leaving its namespace (:func:`init_mark`), with where
      it came from: ``outside``, its parent's mount namespace, and ``by``, its parent's binary —
      and ``pidns``, the init's pid namespace (its container's processes are in it, the ones killed
      after it included; nothing else can enter it). A
      container's init comes from conmon in podman's namespace, an image build step's from podman
      itself there; one from anywhere else is a namespace a process made inside another (any
      container can, ``unshare``), which the join never reads as a container's or a build's
      (:func:`foldyard.monitorlog.attribute`).
    * ``entry``: a process in a mount namespace that isn't the VM's, whose parent is in the VM's own
      (``is_host``), outside the VM's user namespace — podman entering its rootless namespace, as
      every podman command does. Only a process of the VM's can be that parent: nothing in a
      container has one. ``outside`` is the VM's namespace, ``by`` empty.
    * ``other``: any other process whose parent was in another mount namespace, neither of them
      the VM's — the number entered from somewhere other than the VM. An entry never ends and
      numbers are reused, so this is what says a number the VM once entered has gone to someone
      else since (a box container that never shows an init, say). ``by`` empty.

    ``start`` is the process's start time, as Tetragon wrote it."""
    if rec.get("k") != "ev":
        return None
    e = rec.get("e") or {}
    kind = next((k for k in e if k.startswith("process_")), None)
    if kind is None or not isinstance(e[kind], dict):
        return None
    proc, parent = e[kind].get("process") or {}, e[kind].get("parent") or {}
    mnt, pmnt = _ns(proc, "mnt"), _ns(parent, "mnt")
    inode, outside = mnt.get("inum"), pmnt.get("inum")
    if not inode or not outside or outside == inode:
        return None
    fact = {
        "inode": str(inode),
        "start": str(proc.get("start_time") or ""),
        "outside": str(outside),
    }
    mark = init_mark(rec)
    if mark is not None:
        by = posixpath.basename(str(parent.get("binary") or ""))
        pidns = str(_ns(proc, "pid").get("inum") or "")
        return {**fact, "what": mark["what"], "by": by, "pidns": pidns}
    if (
        pmnt.get("is_host") is True
        and mnt.get("is_host") is not True
        and (_ns(proc, "user").get("is_host") is not True)
    ):
        return {**fact, "what": "entry", "by": "", "pidns": ""}
    if pmnt.get("is_host") is not True and mnt.get("is_host") is not True:
        return {**fact, "what": "other", "by": "", "pidns": ""}
    return None


def stamp_ns(stamp: object) -> int | None:
    """A Tetragon timestamp (RFC 3339, UTC, up to nine fractional digits — Go trims trailing
    zeros, so two of them don't compare as text) in nanoseconds; ``None`` when it isn't one."""
    if not isinstance(stamp, str) or not stamp.endswith("Z"):
        return None
    head, _, frac = stamp[:-1].partition(".")
    if frac and not (frac.isdigit() and len(frac) <= 9):
        return None
    try:
        secs = datetime.fromisoformat(head).replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None
    return round(secs) * 10**9 + int(frac.ljust(9, "0") or 0)


# ── a namespace's facts, read ─────────────────────────────────────────────────────────────
#
# Each fact is CLASSIFIED once, from the facts before it (:func:`classify`), and the catalogue
# stores its kind: every later read — the join, retention, the next fact's classification — asks
# only for the newest fact of some kinds about a namespace, never for the history behind it.

HELD = ("entry", "container", "build", "nested")  # kinds that say who holds a namespace from then


def classify(view: NsFacts | _DbFacts, f: dict, seq: int) -> tuple[str, bool]:
    """``(kind, taken)`` of fact ``f`` (:func:`ns_fact`) at ``seq``, from ``view``: the facts
    before it. ``taken``: the number went to someone other than podman's entry.

    * ``entry``: podman entering a namespace from the VM's own. The label ``podman`` holds while it
      is the newest fact about the namespace (:meth:`NsFacts.podman`).
    * ``other``: anything else entering one. Taken when it came from a namespace podman's entry
      holds — podman's side took the number (a box container that never shows an init) — else a
      step in from a container, which takes nothing: the namespace is still alive.
    * An init entering or leaving: ``container`` (conmon, in a namespace podman entered and nothing
      took since) or ``build`` (podman itself there). From anywhere else: ``stepped`` when podman's
      entry holds the namespace it enters — no new namespace there but a step in shaped like an
      init (``unshare -p -f nsenter …``), or one made on a number whose old namespace the facts
      never saw go; nothing tells the two apart, so it reads as a step out, the loud way, and holds
      nothing — or else ``nested``: a namespace made inside another, no mark of a container or a
      build, though it ends whatever held the number before. An init always takes its number."""
    what = f["what"]
    if what == "entry":
        return "entry", False
    if what == "other":
        return "other", view.entered(str(f["outside"]), seq - 1)
    if view.trusted(str(f["outside"]), seq - 1):
        return ("container" if str(f.get("by") or "").startswith("conmon") else "build"), True
    if view.entered(str(f["inode"]), seq - 1):
        return "stepped", True
    return "nested", True


class NsFacts:
    """One boot's namespace facts, each with its kind — the catalogue's, or classified here, forward
    in seq order from the ones before it (:func:`classify`) — read as the join asks."""

    def __init__(self, facts: Iterable[dict]) -> None:
        # inode -> [(seq, kind, what, init start, init pid namespace)], the HELD kinds
        self._held: dict[str, list[tuple[int, str, str, int | None, str]]] = {}
        self._any: dict[str, list[tuple[int, str]]] = {}  # inode -> [(seq, kind)], every fact
        self._trust: dict[str, list[tuple[int, str]]] = {}  # …entries and what took the number
        for f in sorted(facts, key=lambda f: int(f["seq"])):
            seq, inode = int(f["seq"]), str(f["inode"])
            if f.get("kind"):
                kind, taken = str(f["kind"]), bool(f["taken"])
            else:
                kind, taken = classify(self, f, seq)
            self._any.setdefault(inode, []).append((seq, kind))
            if kind == "entry" or taken:
                self._trust.setdefault(inode, []).append((seq, kind))
            if kind in HELD:
                held = (seq, kind, str(f["what"]), stamp_ns(f["start"]), str(f.get("pidns") or ""))
                self._held.setdefault(inode, []).append(held)

    @staticmethod
    def _newest(rows: list, seq: int):
        i = bisect_right(rows, (seq, "\uffff"))
        return rows[i - 1] if i else None

    def holder(self, inode: str, seq: int) -> tuple[int, str, str, int | None, str] | None:
        """The newest fact about who holds ``inode`` at or before ``seq``: ``(seq, kind, what,
        init start, init pid namespace)``, kind one of :data:`HELD`, what ``entry``/``start``/
        ``end``."""
        return self._newest(self._held.get(inode, []), seq)

    def entered(self, inode: str, seq: int) -> bool:
        """Whether podman's entry holds ``inode`` at ``seq``, however much has stepped in since:
        where a step out of a container is read."""
        h = self.holder(inode, seq)
        return h is not None and h[1] == "entry"

    def podman(self, inode: str, seq: int) -> bool:
        """Whether ``inode`` is podman's own namespace at ``seq``: entered from the VM's, and since
        then nothing seen entering it from anywhere else — else the namespace podman entered has
        died and the number gone to someone else, or something stepped in."""
        f = self._newest(self._any.get(inode, []), seq)
        return f is not None and f[1] == "entry"

    def trusted(self, inode: str, seq: int) -> bool:
        """Whether an init coming from ``inode`` at ``seq`` was started by podman: the namespace
        was entered from the VM's, and the number not taken since."""
        f = self._newest(self._trust.get(inode, []), seq)
        return f is not None and f[1] == "entry"

    def start_of(self, inode: str, life: str, below: int) -> tuple | None:
        """The container init start, before ``below``, of the life whose init is in pid namespace
        ``life`` — the start an exit pairs with."""
        return next(
            (f for f in reversed(self._held.get(inode, ())) if f[0] < below and f[1] == "container"
             and f[2] == "start" and f[4] == life),
            None,
        )  # fmt: skip

    def inits(self, inode: str, lo: int, hi: int) -> list[tuple[int, str, int | None, str]]:
        """The container inits entering (``start``) and leaving (``end``) ``inode`` strictly
        between ``lo`` and ``hi``: ``(seq, what, init start, init pid namespace)``."""
        return [
            (q, w, t, p)
            for q, k, w, t, p in self._held.get(inode, ())
            if k == "container" and lo < q < hi
        ]


class _DbFacts:
    """:class:`NsFacts`' questions :func:`classify` asks, answered by the catalogue for one boot —
    inside the writer's transaction, so a fact indexed a moment ago is seen."""

    def __init__(self, conn: sqlite3.Connection, boot: str) -> None:
        self.conn, self.boot = conn, boot

    def _newest_kind(self, inode: str, seq: int, where: str) -> str | None:
        row = self.conn.execute(
            f"SELECT kind FROM nsfact WHERE boot = ? AND inode = ? AND seq <= ? AND ({where}) "
            "ORDER BY seq DESC LIMIT 1",
            (self.boot, inode, seq),
        ).fetchone()
        return row[0] if row else None

    def entered(self, inode: str, seq: int) -> bool:
        held = ", ".join(f"'{k}'" for k in HELD)
        return self._newest_kind(inode, seq, f"kind IN ({held})") == "entry"

    def trusted(self, inode: str, seq: int) -> bool:
        return self._newest_kind(inode, seq, "kind = 'entry' OR taken = 1") == "entry"


class Catalogue:
    """The store's index (see the module doc). One connection per use; any number of readers
    beside the one writer (WAL)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @classmethod
    def open(cls) -> Catalogue:
        path = index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            try:
                return cls._connect(path)
            except sqlite3.OperationalError:
                raise  # locked or busy, not broken: the caller does without it this time
            except sqlite3.DatabaseError:
                # corrupt ("file is not a database"): never for a lock — other connections hold
                # these files open, and the snapshots kept from removed segments can't be rebuilt
                if attempt == 2:
                    raise
                for p in (
                    path,
                    path.with_name(path.name + "-wal"),
                    path.with_name(path.name + "-shm"),
                ):
                    p.unlink(missing_ok=True)  # derived data: rebuilt from the segments
        raise AssertionError("unreachable")

    @classmethod
    def open_or_none(cls) -> Catalogue | None:
        try:
            return cls.open()
        except (sqlite3.Error, OSError):
            return None

    @classmethod
    def _connect(cls, path: Path) -> Catalogue:
        conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        try:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass  # a filesystem without shared memory: the default journal works too
            (version,) = conn.execute("PRAGMA user_version").fetchone()
            if version != _SCHEMA:  # a reader of an up-to-date catalogue takes no write lock
                conn.execute("BEGIN IMMEDIATE")
                try:
                    (version,) = conn.execute("PRAGMA user_version").fetchone()
                    if version != _SCHEMA:  # another opener may have just made it
                        for statement in _SCHEMA_SQL:
                            conn.execute(statement)
                        conn.execute(f"PRAGMA user_version = {_SCHEMA}")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            have = {name for (name,) in conn.execute("SELECT name FROM sqlite_master")}
            if "nsfact" in have and "kind" not in {
                row[1] for row in conn.execute("PRAGMA table_info(nsfact)")
            }:  # facts from before their kinds were stored: derived, so read again (backfill)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("DROP TABLE IF EXISTS nsfact")
                    conn.execute("DROP TABLE IF EXISTS nsfactseg")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                have -= {"nsfact", "nsfactseg"}
            for tables, sql in (
                ({"mark", "markseg"}, _MARK_SQL),
                ({"nsfact", "nsfactseg"}, _FACT_SQL),
            ):
                if tables <= have:
                    continue
                # a catalogue an older foldyard made (0.6: no marks; #136: no facts): beside it
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in sql:
                        conn.execute(statement)
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
        except BaseException:
            conn.close()
            raise
        return cls(conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Catalogue:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── writing ──

    def _put_fact(self, boot: str, seq: int, sid: str, fact: dict) -> None:
        """Classify ``fact`` from the facts before it and store it — or, seen before (a re-read, a
        segment renamed), note where it lives now. A run of entries, or of others alike, is kept
        as its first: podman enters its namespace on every command, a container's runtime enters
        its namespace again and again, and the run's later ones change no answer."""
        c = self.conn
        if c.execute("SELECT 1 FROM nsfact WHERE boot = ? AND seq = ?", (boot, seq)).fetchone():
            c.execute("UPDATE nsfact SET seg = ? WHERE boot = ? AND seq = ?", (sid, boot, seq))
            return
        kind, taken = classify(_DbFacts(c, boot), fact, seq)
        last = c.execute(
            "SELECT kind, taken FROM nsfact WHERE boot = ? AND inode = ? AND seq < ? "
            "ORDER BY seq DESC LIMIT 1",
            (boot, fact["inode"], seq),
        ).fetchone()
        if kind in _RUNS and last == (kind, int(taken)):
            return
        c.execute(
            "INSERT INTO nsfact VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (boot, seq, sid, fact["inode"], fact["what"], fact["start"], fact["outside"],
             fact["by"], fact.get("pidns", ""), kind, int(taken)),
        )  # fmt: skip

    def add(self, sid: str, records: list[dict], indexed: int, exact: bool = False) -> None:
        """Index ``records`` as segment ``sid``'s, which is then indexed up to ``indexed`` bytes
        (``exact``: set to it — the writer, who knows; else only ever raised — a reader)."""
        times = [t for r in records if (t := _ev_time(r))]
        t0, t1 = (min(times), max(times)) if times else ("", "")
        spans: dict[str, tuple[int, int]] = {}
        for r in records:
            if "boot" in r and "seq" in r:
                seq = int(r["seq"])
                lo, hi = spans.get(r["boot"], (seq, seq))
                spans[r["boot"]] = (min(lo, seq), max(hi, seq))
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            for boot, (lo, hi) in spans.items():
                c.execute(
                    "INSERT INTO span VALUES (?, ?, ?, ?) ON CONFLICT(seg, boot) DO UPDATE SET "
                    "lo = MIN(lo, excluded.lo), hi = MAX(hi, excluded.hi)",
                    (sid, boot, lo, hi),
                )
            for r in records:
                k = r.get("k")
                fact = r.get("fact") if k == "ev" and "fact" in r else ns_fact(r)
                if fact is not None and "boot" in r and "seq" in r:
                    self._put_fact(r["boot"], int(r["seq"]), sid, fact)
                mark = r.get("mark") if k == "ev" and "mark" in r else init_mark(r)
                if mark is not None and "boot" in r and "seq" in r:
                    c.execute(
                        "INSERT INTO mark VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(boot, seq) DO "
                        "UPDATE SET seg = excluded.seg",
                        (r["boot"], int(r["seq"]), sid, mark["inode"], mark["what"], mark["start"]),
                    )
                if k == "scopes" and "boot" in r:
                    c.execute(
                        # seen again (a re-read, or a file that changed name): it lives here now
                        "INSERT INTO snap VALUES (?, ?, ?, ?) ON CONFLICT(boot, seq) DO UPDATE "
                        "SET seg = excluded.seg",
                        (r["boot"], int(r["seq"]), sid, json.dumps(r, separators=(",", ":"))),
                    )
                elif k == "engine":
                    for cid, meta in (r.get("containers") or {}).items():
                        c.execute(
                            "INSERT INTO claim VALUES (?, ?, (SELECT COALESCE(MAX(n), 0) + 1 "
                            "FROM claim)) ON CONFLICT(cid) DO UPDATE SET meta = excluded.meta, "
                            "n = excluded.n",
                            (cid, json.dumps(meta)),
                        )
            before = c.execute("SELECT indexed FROM seg WHERE id = ?", (sid,)).fetchone()
            marked = c.execute("SELECT marked FROM markseg WHERE id = ?", (sid,)).fetchone()
            done = c.execute("SELECT done FROM nsfactseg WHERE id = ?", (sid,)).fetchone()
            c.execute(
                "INSERT INTO seg VALUES (?, ?, 0, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                f"indexed = {'excluded.indexed' if exact else 'MAX(indexed, excluded.indexed)'}, "
                "t0 = CASE WHEN t0 = '' OR (excluded.t0 <> '' AND excluded.t0 < t0) "
                "     THEN excluded.t0 ELSE t0 END, "
                "t1 = CASE WHEN excluded.t1 > t1 THEN excluded.t1 ELSE t1 END",
                (sid, indexed, t0, t1),
            )
            # read for marks as far as it is indexed now — unless part of what was indexed before
            # was indexed without them (0.6): catch_up reads that part first
            (now,) = c.execute("SELECT indexed FROM seg WHERE id = ?", (sid,)).fetchone()
            if before is None or (marked is not None and marked[0] >= before[0]):
                c.execute(
                    "INSERT INTO markseg VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "marked = excluded.marked",
                    (sid, now),
                )
            if before is None or (done is not None and done[0] >= before[0]):
                c.execute(
                    "INSERT INTO nsfactseg VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "done = excluded.done",
                    (sid, now),
                )
            c.execute(
                "DELETE FROM claim WHERE n <= (SELECT MAX(n) FROM claim) - ?", (_CLAIMS_KEPT,)
            )
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def sealed(self, sid: str) -> None:
        self.conn.execute("UPDATE seg SET sealed = 1 WHERE id = ?", (sid,))

    def rename(self, old: str, new: str) -> None:
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM seg WHERE id = ?", (new,))
        c.execute("UPDATE seg SET id = ? WHERE id = ?", (new, old))
        c.execute("UPDATE snap SET seg = ? WHERE seg = ?", (new, old))
        c.execute("UPDATE mark SET seg = ? WHERE seg = ?", (new, old))
        c.execute("DELETE FROM markseg WHERE id = ?", (new,))
        c.execute("UPDATE markseg SET id = ? WHERE id = ?", (new, old))
        c.execute("UPDATE nsfact SET seg = ? WHERE seg = ?", (new, old))
        c.execute("DELETE FROM nsfactseg WHERE id = ?", (new,))
        c.execute("UPDATE nsfactseg SET id = ? WHERE id = ?", (new, old))
        c.execute("DELETE FROM span WHERE seg = ?", (new,))
        c.execute("UPDATE span SET seg = ? WHERE seg = ?", (new, old))
        c.execute("COMMIT")

    def drop(self, gone: list[str]) -> None:
        """Forget segments the store removed — but keep the newest snapshot of each boot among
        theirs (the one before the oldest events still stored), and of their namespace facts what
        a read of the events still stored depends on (:meth:`_retain_facts`: podman enters its own
        namespace at boot, a container's init once — the join must still know whose a namespace
        is, however long ago that was said)."""
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        c.executemany("DELETE FROM seg WHERE id = ?", [(g,) for g in gone])
        c.executemany("DELETE FROM span WHERE seg = ?", [(g,) for g in gone])
        c.executemany("DELETE FROM mark WHERE seg = ?", [(g,) for g in gone])
        c.executemany("DELETE FROM markseg WHERE id = ?", [(g,) for g in gone])
        c.executemany("DELETE FROM nsfactseg WHERE id = ?", [(g,) for g in gone])
        # a boot's facts go once the store holds nothing of that boot — never the running boot's:
        # the one whose fact was indexed last (the store is in boot order — the relay's spool
        # paths sort by epoch — and the catalogue indexes it in that order). Right after a seal the
        # live segment may hold no record of the running boot yet, only an engine claim: its facts
        # are what its next events are read by.
        c.execute(
            "DELETE FROM nsfact WHERE seg NOT IN (SELECT id FROM seg) "
            "AND boot NOT IN (SELECT boot FROM span) "
            "AND boot IS NOT (SELECT boot FROM nsfact ORDER BY rowid DESC LIMIT 1)"
        )
        orphan = "seg NOT IN (SELECT id FROM seg)"
        c.execute(
            f"DELETE FROM snap WHERE {orphan} AND seq < (SELECT MAX(o.seq) FROM snap o "
            f"WHERE o.boot = snap.boot AND o.{orphan})"
        )
        c.execute(
            f"DELETE FROM snap WHERE {orphan} AND rowid NOT IN "
            f"(SELECT rowid FROM snap WHERE {orphan} ORDER BY rowid DESC LIMIT ?)",
            (_ORPHANS_KEPT,),
        )
        self._retain_facts()
        c.execute("COMMIT")

    def _retain_facts(self) -> None:
        """Of the facts from segments already gone, keep what a read of anything still stored — or
        the classification of a fact still to come — can ask (every fact carries its kind, so no
        history behind it): per namespace the newest holder, the newest fact, and the newest entry
        or fact that took the number; and every fact after the snapshot kept from before the
        boot's oldest stored record, where a read's window starts (a life's start matters to the
        join only inside the window). Inside :meth:`drop`'s transaction."""
        c = self.conn
        rows: dict[tuple[str, str], list[tuple]] = {}
        for row in c.execute(
            "SELECT boot, inode, seq, kind, taken FROM nsfact "
            "WHERE seg NOT IN (SELECT id FROM seg) ORDER BY boot, inode, seq"
        ):
            rows.setdefault((row[0], row[1]), []).append(row[2:])
        windows: dict[str, int] = {}
        drop = []
        for (boot, _), facts in rows.items():
            if boot not in windows:
                (lo,) = c.execute("SELECT MIN(lo) FROM span WHERE boot = ?", (boot,)).fetchone()
                (snap,) = c.execute(
                    "SELECT MAX(seq) FROM snap WHERE boot = ? AND seq < ?",
                    (boot, 1 << 62 if lo is None else lo),
                ).fetchone()
                windows[boot] = -1 if snap is None else int(snap)
            keep = {seq for seq, *_ in facts if seq > windows[boot]}

            def newest(test, rows=facts) -> tuple | None:
                return next((r for r in reversed(rows) if test(r)), None)

            picks = [
                newest(lambda r: r[1] in HELD),
                facts[-1],
                newest(lambda r: r[1] == "entry" or r[2]),  # (seq, kind, taken)
            ]
            keep |= {r[0] for r in picks if r}
            drop += [(boot, seq) for seq, *_ in facts if seq not in keep]
        c.executemany("DELETE FROM nsfact WHERE boot = ? AND seq = ?", drop)

    def catch_up(self, segs: list[Segment] | None = None, whole: bool = False) -> None:
        """Index whatever of ``segs`` (default: the whole store) isn't yet: a store an older
        foldyard wrote, a catalogue that was lost, or a crash between the two writes. ``whole``:
        ``segs`` is a full listing of the store (as the default is), so a row for a segment it
        doesn't show may be one the store no longer has."""
        listing = segs is None or whole
        segs = segments() if segs is None else segs
        known = {
            sid: (indexed, sealed)
            for sid, indexed, sealed in self.conn.execute("SELECT id, indexed, sealed FROM seg")
        }
        self._backfill_marks(segs)
        self._backfill_facts(segs)
        if listing and segs:
            # Gone from the store without this catalogue seeing it go (an older foldyard rotated
            # or removed it): older than every segment listed, or an older foldyard's live file
            # under a name it no longer has. Never anything newer — a reader's listing can be a
            # moment behind the writer's next segment.
            # (only below the oldest SEALED one: a live segment sealed between the listing and
            # the read of the new live file's first line is missing from the listing, and is
            # older than that new one)
            here = {s.id for s in segs}
            # No sealed segment listed (the budget took them all with the catalogue locked): no
            # age-based drop until one is; the stale rows only keep a few snapshots a while.
            oldest = min((s.id for s in segs if not s.live), default="")
            gone = [
                sid for sid in known if sid not in here and (sid.startswith(LEGACY) or sid < oldest)
            ]
            if gone:
                self.drop(gone)
        for seg in segs:
            indexed, sealed = known.get(seg.id, (0, 0))
            if sealed:
                continue
            if seg.live and _header(live_path()) != seg.id:
                # sealed since it was listed: the live file is another segment now, and this one
                # is indexed under the id it has by the next catch-up (an older foldyard's
                # `legacy-<inode>` gets a new one) — never a row for a name nothing has any more
                continue
            size = raw_size(seg)
            if indexed < size:
                batch: list[dict] = []
                end = indexed
                for line, end in _lines(seg, indexed):
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    if rec.get("k") == "ev":  # all the catalogue keeps of an event
                        mark, fact = init_mark(rec), ns_fact(rec)
                        rec = {
                            "k": "ev",
                            **{k: rec[k] for k in ("boot", "seq") if k in rec},
                            "e": {"time": _ev_time(rec)},
                            **({"mark": mark} if mark else {}),
                            **({"fact": fact} if fact else {}),
                        }
                    batch.append(rec)
                    if len(batch) >= 20_000:
                        self.add(seg.id, batch, end)
                        batch = []
                self.add(seg.id, batch, end)
            if not seg.live:
                self.sealed(seg.id)

    def _backfill_marks(self, segs: list[Segment]) -> None:
        """Read the marks out of what an older catalogue indexed before marks were kept — only the
        lines that can hold one are parsed, so a store of gigabytes costs one pass of gunzip."""
        owed = {
            sid: (marked, indexed)
            for sid, marked, indexed in self.conn.execute(
                "SELECT seg.id, COALESCE(markseg.marked, 0), seg.indexed FROM seg "
                "LEFT JOIN markseg ON markseg.id = seg.id "
                "WHERE COALESCE(markseg.marked, 0) < seg.indexed"
            )
        }
        for seg in segs:
            if seg.id not in owed:
                continue
            marked, indexed = owed[seg.id]
            batch: list[dict] = []
            for line, end in _lines(seg, marked):
                if end > indexed:
                    break
                if not any(n in line for n in _MARK_NEEDLES):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ok = isinstance(rec, dict) and "boot" in rec and "seq" in rec  # as `add` asks
                if ok and init_mark(rec) is not None:
                    batch.append(rec)
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                for r in batch:
                    m = init_mark(r)
                    assert m is not None
                    c.execute(
                        "INSERT INTO mark VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(boot, seq) DO "
                        "UPDATE SET seg = excluded.seg",
                        (r["boot"], int(r["seq"]), seg.id, m["inode"], m["what"], m["start"]),
                    )
                c.execute(
                    "INSERT INTO markseg VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "marked = excluded.marked",
                    (seg.id, indexed),
                )
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def _backfill_facts(self, segs: list[Segment]) -> None:
        """Read the facts out of what an older catalogue indexed before facts were kept (as
        :meth:`_backfill_marks` does the marks): only the lines that can hold one are parsed."""
        owed = {
            sid: (done, indexed)
            for sid, done, indexed in self.conn.execute(
                "SELECT seg.id, COALESCE(nsfactseg.done, 0), seg.indexed FROM seg "
                "LEFT JOIN nsfactseg ON nsfactseg.id = seg.id "
                "WHERE COALESCE(nsfactseg.done, 0) < seg.indexed"
            )
        }
        for seg in segs:
            if seg.id not in owed:
                continue
            done, indexed = owed[seg.id]
            facts: list[tuple[str, int, dict]] = []
            for line, end in _lines(seg, done):
                if end > indexed:
                    break
                if not _may_hold_fact(line):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                f = ns_fact(rec) if isinstance(rec, dict) else None
                if f is None or "boot" not in rec or "seq" not in rec:
                    continue
                facts.append((rec["boot"], int(rec["seq"]), f))
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                for boot, seq, f in facts:  # in store order: each classified by those before it
                    self._put_fact(boot, seq, seg.id, f)
                c.execute(
                    "INSERT INTO nsfactseg VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "done = excluded.done",
                    (seg.id, indexed),
                )
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    # ── reading ──

    def _snap(self, sql: str, args: tuple) -> dict | None:
        row = self.conn.execute(sql, args).fetchone()
        return json.loads(row[0]) if row else None

    def before(self, boot: str, seq: int) -> dict | None:
        """The last snapshot of ``boot`` before ``seq``."""
        return self._snap(
            "SELECT rec FROM snap WHERE boot = ? AND seq < ? ORDER BY seq DESC LIMIT 1", (boot, seq)
        )

    def after(self, boot: str, seq: int) -> dict | None:
        """The first snapshot of ``boot`` after ``seq``."""
        return self._snap(
            "SELECT rec FROM snap WHERE boot = ? AND seq > ? ORDER BY seq LIMIT 1", (boot, seq)
        )

    def marks(self, boot: str, lo: int, hi: int) -> list[dict]:
        """``boot``'s container-init marks strictly between seqs ``lo`` and ``hi``, as records the
        join reads (``{"k": "mark", "boot", "seq", "inode", "what", "start"}``)."""
        rows = self.conn.execute(
            "SELECT seq, inode, what, start FROM mark WHERE boot = ? AND seq > ? AND seq < ? "
            "ORDER BY seq",
            (boot, lo, hi),
        )
        return [
            {"k": "mark", "boot": boot, "seq": seq, "inode": inode, "what": what, "start": start}
            for seq, inode, what, start in rows
        ]

    _FACT_COLS = "seq, inode, what, start, outside, by, pidns, kind, taken"

    @staticmethod
    def _fact(boot: str, row: tuple) -> dict:
        seq, inode, what, start, outside, by, pidns, kind, taken = row
        return {"k": "fact", "boot": boot, "seq": seq, "inode": inode, "what": what,
                "start": start, "outside": outside, "by": by, "pidns": pidns, "kind": kind,
                "taken": taken}  # fmt: skip

    def facts(self, boot: str, lo: int, hi: int) -> list[dict]:
        """``boot``'s namespace facts strictly between seqs ``lo`` and ``hi``, as records the join
        reads (``{"k": "fact", "boot", "seq", "inode", "what", "start", "outside", "by", "pidns",
        "kind", "taken"}``)."""
        return [
            self._fact(boot, row)
            for row in self.conn.execute(
                f"SELECT {self._FACT_COLS} FROM nsfact WHERE boot = ? AND seq > ? AND seq < ? "
                "ORDER BY seq",
                (boot, lo, hi),
            )
        ]

    def prior(self, boot: str, inode: str, below: int) -> list[dict]:
        """What :class:`NsFacts` asks about namespace ``inode`` from before seq ``below`` — the
        facts :meth:`_retain_facts` keeps: the newest holder, the newest fact, and the newest entry
        or fact that took the number (for a record the catalogue hasn't classified yet)."""
        held = ", ".join(f"'{k}'" for k in HELD)
        out: dict[int, dict] = {}

        def newest(where: str) -> None:
            row = self.conn.execute(
                f"SELECT {self._FACT_COLS} FROM nsfact WHERE boot = ? AND inode = ? AND seq < ? "
                f"AND ({where}) ORDER BY seq DESC LIMIT 1",
                (boot, inode, below),
            ).fetchone()
            if row is not None:
                out[row[0]] = self._fact(boot, row)

        newest(f"kind IN ({held})")
        newest("1")
        newest("kind = 'entry' OR taken = 1")
        return list(out.values())

    def last_snapshot(self, boot: str) -> int:
        (seq,) = self.conn.execute("SELECT MAX(seq) FROM snap WHERE boot = ?", (boot,)).fetchone()
        return -1 if seq is None else int(seq)

    def holding(self, boot: str, seq: int) -> set[str]:
        """The segments whose records of ``boot`` span ``seq``."""
        rows = self.conn.execute(
            "SELECT seg FROM span WHERE boot = ? AND lo <= ? AND hi >= ?", (boot, seq, seq)
        )
        return {sid for (sid,) in rows}

    def unindexed(self, segs: list[Segment]) -> set[str]:
        """Those of ``segs`` this catalogue hasn't indexed to their end: what it says of them
        (:meth:`holding`) is not yet everything they hold."""
        known = {
            sid: (indexed, sealed)
            for sid, indexed, sealed in self.conn.execute("SELECT id, indexed, sealed FROM seg")
        }
        out = set()
        for s in segs:
            indexed, sealed = known.get(s.id, (-1, 0))
            if indexed < 0 or (not sealed and indexed < raw_size(s)):
                out.add(s.id)
        return out

    def span(self, boot: str, segs: list[Segment]) -> tuple[int, int] | None:
        """The lowest and highest seq of ``boot`` that ``segs`` hold, as far as this catalogue
        has indexed them; ``None`` when none does."""
        ids = {s.id for s in segs}
        rows = [
            (lo, hi)
            for sid, lo, hi in self.conn.execute(
                "SELECT seg, lo, hi FROM span WHERE boot = ?", (boot,)
            )
            if sid in ids
        ]
        return (min(r[0] for r in rows), max(r[1] for r in rows)) if rows else None

    def claims(self) -> dict[str, dict | None]:
        """Every container's latest engine claim (name, compose project/service — claims)."""
        return {
            cid: json.loads(meta)
            for cid, meta in self.conn.execute("SELECT cid, meta FROM claim ORDER BY n")
        }

    def bounds(self) -> dict[str, tuple[str, str]]:
        """Each segment's first and last event time ("" unknown)."""
        return {sid: (t0, t1) for sid, t0, t1 in self.conn.execute("SELECT id, t0, t1 FROM seg")}
