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
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import time
import tomllib
import zlib
from collections.abc import Iterator
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
_SCHEMA_SQL = (
    "DROP TABLE IF EXISTS seg",
    "DROP TABLE IF EXISTS snap",
    "DROP TABLE IF EXISTS claim",
    "DROP TABLE IF EXISTS span",
    "CREATE TABLE seg (id TEXT PRIMARY KEY, indexed INTEGER NOT NULL, sealed INTEGER NOT NULL, "
    "t0 TEXT NOT NULL, t1 TEXT NOT NULL)",
    "CREATE TABLE snap (boot TEXT NOT NULL, seq INTEGER NOT NULL, seg TEXT NOT NULL, "
    "rec TEXT NOT NULL, PRIMARY KEY (boot, seq))",
    "CREATE TABLE claim (cid TEXT PRIMARY KEY, meta TEXT NOT NULL, n INTEGER NOT NULL)",
    "CREATE TABLE span (seg TEXT NOT NULL, boot TEXT NOT NULL, lo INTEGER NOT NULL, "
    "hi INTEGER NOT NULL, PRIMARY KEY (seg, boot))",
)


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
            c.execute(
                "INSERT INTO seg VALUES (?, ?, 0, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                f"indexed = {'excluded.indexed' if exact else 'MAX(indexed, excluded.indexed)'}, "
                "t0 = CASE WHEN t0 = '' OR (excluded.t0 <> '' AND excluded.t0 < t0) "
                "     THEN excluded.t0 ELSE t0 END, "
                "t1 = CASE WHEN excluded.t1 > t1 THEN excluded.t1 ELSE t1 END",
                (sid, indexed, t0, t1),
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
        c.execute("DELETE FROM span WHERE seg = ?", (new,))
        c.execute("UPDATE span SET seg = ? WHERE seg = ?", (new, old))
        c.execute("COMMIT")

    def drop(self, gone: list[str]) -> None:
        """Forget segments the store removed — but keep the newest snapshot of each boot among
        theirs: the one before the oldest events still stored."""
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        c.executemany("DELETE FROM seg WHERE id = ?", [(g,) for g in gone])
        c.executemany("DELETE FROM span WHERE seg = ?", [(g,) for g in gone])
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
        c.execute("COMMIT")

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
                        rec = {
                            "k": "ev",
                            **{k: rec[k] for k in ("boot", "seq") if k in rec},
                            "e": {"time": _ev_time(rec)},
                        }
                    batch.append(rec)
                    if len(batch) >= 20_000:
                        self.add(seg.id, batch, end)
                        batch = []
                self.add(seg.id, batch, end)
            if not seg.live:
                self.sealed(seg.id)

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
