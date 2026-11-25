"""monitorevidence.py — the records around a step out of a container, kept beside the guest
monitor's store and past its budget (docs/monitor-pipeline.md §3.3).

Once a crossing's segment leaves the store, a time and a container are all that remain of it. So
each one's WINDOW — its boot's records from :data:`SIDE_SECONDS` before it to as long after — is
copied to ``logs/evidence/<id>/``, which no reader of the store lists (sparing the segments in place
would leave holes in a store whose hooks and retention take it to be contiguous, and a hook whose
position fell in one would wait for good):

* ``records.jsonl.gz``: the window's lines, as the store had them;
* ``context.jsonl.gz``: what joining them takes from the catalogue (:func:`monitorlog.around`) and
  the engine's claims, so the evidence is read by itself;
* ``meta.json``: the crossings in it, their verdicts, and what is missing.

Written aside and renamed, so a directory is there whole or not at all. The pull calls :func:`keep`
after each append, once what it stored is judged and before retention removes anything.

**The cap** is the operator's (``evidence_mb`` in the host-only ``monitor-store.toml``), its shares
fractions of it: a window's, and a boot's, within which the FIRST crossings win — an escaped process
must not flush out how it got out by crossing again; those past the share are counted. **Full**, a
new window is PENDING: it stays in the store, the operator is told, and it is copied once there is
room. Only when retention is about to remove its records does the oldest boots' evidence make way
(:func:`_victims`)."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import tomllib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import config, monitorlog, monitorstore

CAP_MB = 256  # default cap on a project's evidence
CAP_MIN_MB, CAP_MAX_MB = 64, 4096
SIDE_SECONDS = 60  # a window's reach either side of its crossing, by the events' own time
CLOSE_SECONDS = 180.0  # a window is whole this long after the snapshot that settled its crossing
_BOOT, _WINDOW, _LEAST = 4, 16, 256  # shares of the cap: a boot's, a window's, the least worth it
_RATIO = 32  # raw bytes a compressed one is taken to hold, to begin with
_LISTED = 200  # crossings named in one window's meta.json (the rest are counted)
_LISTING = 250  # bytes one takes there, reserved for each not judged yet
_NO_ROOM = "a snapshot after it, which its share of the cap had no room for"


def root() -> Path:
    return monitorstore.store_dir() / "evidence"


def _state_file() -> Path:
    return root() / "state.json"


def cap_setting() -> tuple[int, str]:
    """``(bytes, note)``: the cap from the operator's settings (0 = keep none), as
    :func:`monitorstore.budget_setting` reads the store's budget."""
    path = monitorstore.settings_file()
    try:
        doc = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return CAP_MB << 20, ""  # unreadable: the budget's own note says so
    raw = doc.get("evidence_mb", CAP_MB)
    if not isinstance(raw, int) or isinstance(raw, bool):
        return CAP_MB << 20, f"`evidence_mb` must be a whole number of MB: using {CAP_MB} MB"
    if raw == 0:
        return 0, ""
    mb = min(max(raw, CAP_MIN_MB), CAP_MAX_MB)
    note = "" if mb == raw else f"`evidence_mb = {raw}` is outside {CAP_MIN_MB}–{CAP_MAX_MB}"
    return mb << 20, note + (f": using {mb} MB" if note else "")


def cap() -> int:
    return cap_setting()[0]


# ── what is kept ──────────────────────────────────────────────────────────────────────


@dataclass
class Evidence:
    id: str
    boot: str
    seq: int  # the crossing it was kept for
    time: str
    lo: int  # the window, in its boot's sequence numbers
    hi: int
    events: int = 0
    recv: float = 0.0  # when it reached your computer: how boots are ordered by age
    crossings: list[dict] = field(default_factory=list)  # judged to be: {seq, time, container}
    more: int = 0  # …past those listed
    unjudged: list[int] = field(default_factory=list)  # what may be one, not judged yet
    open: bool = False  # no snapshot after it yet: its last events may be unnamed
    missing: list[str] = field(default_factory=list)
    size: int = 0  # on disk; measured, never stored

    @property
    def path(self) -> Path:
        return root() / self.id

    @property
    def confirmed(self) -> bool:
        return bool(self.crossings)


_FIELDS = frozenset(Evidence.__dataclass_fields__) - {"id", "size"}


def _size(path: Path) -> int:
    total = 0
    for f in path.iterdir():
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def kept(known: dict[str, Evidence | None] | None = None) -> list[Evidence]:
    """Every piece of evidence, oldest boot first, then in sequence — ``known`` as given, the rest
    read."""
    known = known or {}
    try:
        names = sorted(os.listdir(root()))
    except OSError:
        return []
    paths = (root() / name for name in names if not name.startswith("."))
    read = (known[p.name] if p.name in known else _piece(p) for p in paths if p.is_dir())
    return _in_order([e for e in read if e is not None])


def _piece(path: Path) -> Evidence | None:
    try:
        doc = json.loads((path / "meta.json").read_text())
        known = {k: v for k, v in doc.items() if k in _FIELDS}
        return Evidence(id=path.name, **known, size=_size(path))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _in_order(every: list[Evidence]) -> list[Evidence]:
    first: dict[str, float] = {}
    for e in every:
        first[e.boot] = min(first.get(e.boot, float("inf")), e.recv)
    return sorted(every, key=lambda e: (first[e.boot], e.boot, e.seq))


class Held:
    """What is kept, read at most once in a :func:`keep` — not at all when there is nothing to do —
    and followed through what that changes."""

    def __init__(self) -> None:
        self._every: list[Evidence] | None = None
        self._ones: dict[str, Evidence | None] = {}  # read by themselves, before all were

    @property
    def every(self) -> list[Evidence]:
        if self._every is None:
            self._every = kept(self._ones)
        return self._every

    def put(self, e: Evidence) -> None:
        """``e`` as it is now, in place of what was read of it."""
        self._ones[e.id] = e
        if self._every is not None:
            self._every = _in_order([*(x for x in self._every if x.id != e.id), e])

    def drop(self, e: Evidence) -> None:
        if self._every is not None:
            self._every = [x for x in self._every if x.id != e.id]

    def one(self, ident: str) -> Evidence | None:
        if self._every is not None:
            return next((e for e in self._every if e.id == ident), None)
        if ident not in self._ones:
            self._ones[ident] = _piece(root() / ident)
        return self._ones[ident]


@dataclass
class State:
    done: dict[str, int] = field(default_factory=dict)  # boot -> the last crossing dealt with
    past: dict[str, int] = field(default_factory=dict)  # boot -> crossings past its share
    pending: list[dict] = field(default_factory=list)  # windows waiting for room
    say: list[dict] = field(default_factory=list)  # for the supervisor to log ({text, notify})
    full: bool = False  # said to be full
    making: str = ""  # the boot whose window others are making way for
    unsettled: list[str] | None = None  # ids still waiting for a snapshot or a verdict (None: any)
    error: str = ""

    @classmethod
    def load(cls) -> State:
        try:
            doc = json.loads(_state_file().read_text())
            return cls(**{k: v for k, v in doc.items() if k in cls.__dataclass_fields__})
        except (OSError, ValueError, TypeError, AttributeError):
            return cls()

    def save(self) -> None:
        root().mkdir(mode=0o700, parents=True, exist_ok=True)
        _write(_state_file(), json.dumps(asdict(self)).encode())

    def count(self, boot: str, seqs: list[int]) -> None:
        """Crossings not kept: past what their boot may take."""
        self.past[boot] = self.past.get(boot, 0) + len(seqs)

    def tell(self, text: str, notify: bool = False) -> None:
        self.say = [*self.say, {"text": text, "notify": notify}][-50:]


def _write(path: Path, data: bytes) -> None:
    """Written aside and renamed, into a directory that must be there: one the operator removed
    meanwhile stays removed."""
    tmp = path.with_name(f".{path.name}.tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        f.write(data)
    tmp.replace(path)


def _id(boot: str, seq: int) -> str:
    plain = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", boot)
    return f"{boot if plain else hashlib.sha256(boot.encode()).hexdigest()[:32]}-{seq}"


# ── keeping it: after each append, before retention ───────────────────────────────────


def keep(cat: monitorstore.Catalogue, now: float, going: frozenset[str] = frozenset()) -> None:
    """Copy out every window that is whole, and what ``going`` — the segments retention is about
    to remove — would take from one, whole or not. ``now`` is your computer's clock."""
    limit = cap()
    boots = [b for (b,) in cat.conn.execute("SELECT DISTINCT boot FROM crossing")]
    if not limit or (not boots and not root().exists()):
        return
    st = State.load()
    fresh = st.unsettled is None  # no state of this version yet: the one before left `.new-<id>`
    before = asdict(st)
    try:
        held = Held()
        for stale in (*(root() / ".new").glob("*"), *(root().glob(".new-*") if fresh else ())):
            shutil.rmtree(stale, ignore_errors=True)  # a crash while writing one
        _confirm(cat, st, held, limit)
        _finish(st, held, limit)
        boots.sort(key=lambda b: _age(cat, b))
        while True:  # one window of a boot at a time: what it takes decides what the next may
            for boot in boots:
                _note(cat, st, held, boot, now, limit, going)
            if not _flush(cat, st, held, limit, going):
                break
        st.error = ""
    except Exception as e:
        st.error = f"{type(e).__name__}: {e}"[:300]
        raise
    finally:
        if asdict(st) != before:
            st.save()


def unable(why: str) -> None:
    """Note that nothing could be copied out: :func:`note` says so until it can."""
    st = State.load()
    if st.error != why:
        st.error = why
        st.save()


def _age(cat: monitorstore.Catalogue, boot: str) -> int:
    (row,) = cat.conn.execute("SELECT MIN(rowid) FROM span WHERE boot = ?", (boot,)).fetchone()
    return -1 if row is None else int(row)


def _ended(cat: monitorstore.Catalogue, boot: str) -> bool:
    """Whether the store already holds a later boot than ``boot``."""
    row = cat.conn.execute("SELECT boot FROM span ORDER BY rowid DESC LIMIT 1").fetchone()
    return row is not None and row[0] != boot


def _confirm(cat: monitorstore.Catalogue, st: State, held: Held, limit: int) -> None:
    """Take what has come since for what was kept without it: the snapshot after a window, and the
    verdicts on what may have been a crossing — evidence that turns out to hold none goes."""
    if st.unsettled is None:
        st.unsettled = [e.id for e in held.every if _waits(e)]
    for ident in list(st.unsettled):
        e = held.one(ident)
        if e is None or not _waits(e) or _forgotten(cat, e.boot):
            st.unsettled.remove(ident)
            continue
        before = _meta(e)
        rows = {r["seq"]: r for r in _crossings(cat, e.boot, e.lo, e.hi)}
        judged = [rows[q] for q in e.unjudged if q in rows and rows[q]["verdict"] is not None]
        done = {r["seq"] for r in judged}
        e.unjudged = [q for q in e.unjudged if q not in done]
        _list(e, [r for r in judged if r["verdict"] == 1])
        try:
            if not e.crossings and not e.unjudged:
                shutil.rmtree(e.path)
                held.drop(e)
                st.tell(
                    f"the evidence kept at {_at(e.time)} was removed: judged not to be a crossing"
                )
                continue
            if e.open and _NO_ROOM not in e.missing and cat.after(e.boot, e.hi) is not None:
                every = held.every
                mine = sum(x.size for x in every if x.boot == e.boot)
                _close(cat, e, min(limit // _WINDOW - e.size, limit // _BOOT - mine,
                                   limit - sum(x.size for x in every)))  # fmt: skip
            if _meta(e) != before:
                _write(e.path / "meta.json", _meta(e))
                e.size = _size(e.path)
                held.put(e)
        except FileNotFoundError:
            held.drop(e)  # the operator removed it meanwhile
            continue
        if not _waits(e):
            st.unsettled.remove(ident)


def _waits(e: Evidence) -> bool:
    return bool(e.unjudged) or (e.open and _NO_ROOM not in e.missing)


def _forgotten(cat: monitorstore.Catalogue, boot: str) -> bool:
    """Whether the store holds nothing of ``boot`` any more: nothing about it will come."""
    for table in ("span", "crossing"):
        if cat.conn.execute(f"SELECT 1 FROM {table} WHERE boot = ? LIMIT 1", (boot,)).fetchone():
            return False
    return True


def _list(e: Evidence, rows: list[dict]) -> None:
    named = [{k: r[k] for k in ("seq", "time", "container")} for r in rows]
    room = max(0, _LISTED - len(e.crossings))
    e.crossings, e.more = e.crossings + named[:room], e.more + len(named[room:])


def _close(cat: monitorstore.Catalogue, e: Evidence, room: int) -> None:
    """Add to ``e``'s context the snapshot after it, and the namespace facts up to that — in
    ``room`` more bytes, what is left of its shares."""
    snap = cat.after(e.boot, e.hi)
    assert snap is not None
    held = list(_jsonl(e.path / "context.jsonl.gz"))
    claims = {}
    for r in held:
        claims |= r.get("containers") or {}
    every = cat.claims()
    for cid in {cid for ids in snap["map"].values() for cid in ids} & every.keys():
        claims[cid] = every[cid]
    facts = cat.facts(e.boot, e.hi, int(snap["seq"]))
    had = {int(r["seq"]) for r in held if r.get("k") == "fact"}
    new = [snap, *(f for f in facts if int(f["seq"]) not in had)]
    tmp = e.path / ".context.tmp"
    with _gz(tmp) as f:
        for r in [*(r for r in held if r.get("k") != "engine"), *new,
                  {"k": "engine", "containers": claims}]:  # fmt: skip
            f.write(monitorstore._dump(r))
    context = e.path / "context.jsonl.gz"
    if tmp.stat().st_size - context.stat().st_size > room - len(e.unjudged[:_LISTED]) * _LISTING:
        tmp.unlink()
        e.missing.append(_NO_ROOM)
        return
    tmp.replace(context)
    e.open = False


def _jsonl(path: Path) -> Iterator[dict]:
    with gzip.open(path, "rb") as f:
        for line in f:
            yield json.loads(line)


def _note(
    cat: monitorstore.Catalogue,
    st: State,
    held: Held,
    boot: str,
    now: float,
    limit: int,
    going: frozenset[str],
) -> None:
    """Make a pending window of ``boot``'s next crossing not yet in one, if that window is whole —
    one at a time, once the ones before it have been measured: what they take of the boot's share
    decides whether there is any left for it."""
    done = st.done.get(boot)
    if done is None:  # the state was lost: nothing kept is kept again
        done = max((e.hi for e in held.every if e.boot == boot), default=-1)
    ended = _ended(cat, boot)
    rows = cat.conn.execute(
        # one not judged by then is kept unconfirmed
        "SELECT seq, time FROM crossing WHERE boot = ? AND seq > ? AND "
        "(verdict IS NULL OR verdict = 1) ORDER BY seq",
        (boot, done),
    ).fetchall()
    if not rows:
        st.done.setdefault(boot, done)
        return
    mine = [e for e in held.every if e.boot == boot]
    for seq, when in rows:
        seq, t = int(seq), monitorstore.stamp_ns(when)
        waiting = [p for p in st.pending if p["boot"] == boot]
        spans = [(e.lo, e.hi) for e in mine] + [(p["lo"], p["hi"]) for p in waiting]
        if any(lo <= seq <= hi for lo, hi in spans):
            st.done[boot] = seq
            continue
        settled = cat.after(boot, seq)
        lag = now - float(settled.get("recv") or 0.0) if settled else 0.0
        forced = bool(going & _segs_from(cat, boot, seq, t))
        # (a negative lag: your computer's clock was set back)
        whole = ended or (settled is not None and not 0 <= lag < CLOSE_SECONDS)
        if not (whole or forced):
            return
        if any("size" not in p for p in waiting):
            return
        taken = sum(e.size for e in mine) + sum(p["size"] for p in waiting)
        if limit // _BOOT - taken < limit // _LEAST:
            st.count(boot, [seq])
            st.done[boot] = seq
            continue
        floor = max((hi for _, hi in spans if hi < seq), default=-1)
        lo, hi = _window(cat, boot, seq, t, floor)
        st.pending.append({
            "boot": boot, "seq": seq, "time": when, "lo": lo, "hi": hi, "recv": now,
            "cut": not whole, "floor": floor,
        })  # fmt: skip
        st.done[boot] = seq
        return


# A segment's first and last event times are compared as text, and Tetragon trims trailing zeros
# ("…:30Z" sorts after "…:30.1Z"): they are only good to the second.
_TEXT_NS = 10**9


def _segs_from(cat: monitorstore.Catalogue, boot: str, seq: int, t: int | None) -> set[str]:
    """The segments holding what a window at ``seq`` starts with."""
    out = set()
    for sid, t1 in cat.conn.execute(
        "SELECT span.seg, seg.t1 FROM span JOIN seg ON seg.id = span.seg "
        "WHERE span.boot = ? AND span.lo <= ?",
        (boot, seq),
    ):
        end = monitorstore.stamp_ns(t1)
        if t is None or end is None or end >= t - SIDE_SECONDS * 10**9 - _TEXT_NS:
            out.add(sid)
    return out


def _segs_of(cat: monitorstore.Catalogue, boot: str, lo: int, hi: int) -> set[str]:
    rows = cat.conn.execute(
        "SELECT seg FROM span WHERE boot = ? AND lo <= ? AND hi >= ?", (boot, hi, lo)
    )
    return {sid for (sid,) in rows}


def _lines(segs: Iterable[monitorstore.Segment], boot: str) -> Iterator[tuple[bytes, dict]]:
    """``boot``'s lines in ``segs``, as stored, each with its record."""
    needle = json.dumps(boot).encode()
    for seg in segs:
        for line, _ in monitorstore._lines(seg, 0):
            if needle not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("boot") == boot:
                yield line, rec


def _window(
    cat: monitorstore.Catalogue, boot: str, seq: int, t: int | None, floor: int
) -> tuple[int, int]:
    """The window of the crossing at ``seq``, past ``floor``: the sequence numbers from its boot's
    first event within :data:`SIDE_SECONDS` before it to the last within as long after."""
    if t is None:
        return seq, seq
    side = SIDE_SECONDS * 10**9
    segs = [s for s in monitorstore.segments() if s.id in _near(cat, boot, seq, t)]
    lo = hi = seq
    for _, rec in _lines(segs, boot):
        if rec.get("k") != "ev" or "seq" not in rec:
            continue
        at = monitorstore.stamp_ns((rec.get("e") or {}).get("time"))
        if at is not None and t - side <= at <= t + side and int(rec["seq"]) > floor:
            lo, hi = min(lo, int(rec["seq"])), max(hi, int(rec["seq"]))
    return lo, hi


def _near(cat: monitorstore.Catalogue, boot: str, seq: int, t: int) -> set[str]:
    """The segments whose events' times reach into the window — and no more of them than a
    window's share can hold, nearest first (a guest's clock can jump)."""
    side = SIDE_SECONDS * 10**9 + _TEXT_NS
    rows = cat.conn.execute(
        "SELECT span.seg, span.lo, span.hi, seg.t0, seg.t1 FROM span JOIN seg ON seg.id = span.seg "
        "WHERE span.boot = ?",
        (boot,),
    ).fetchall()
    near = []
    for sid, lo, hi, t0, t1 in rows:
        start, end = monitorstore.stamp_ns(t0), monitorstore.stamp_ns(t1)
        if start is None or end is None or (start <= t + side and end >= t - side):
            near.append((max(lo - seq, seq - hi, 0), sid))
    most = 2 + cap() // _WINDOW * _RATIO // monitorstore.SEGMENT_BYTES
    return {sid for _, sid in sorted(near)[:most]}


# ── the copy ──────────────────────────────────────────────────────────────────────────


def _flush(
    cat: monitorstore.Catalogue, st: State, held: Held, limit: int, going: frozenset[str]
) -> bool:
    """Copy the pending windows there is room for, oldest first — and those ``going`` would take
    from, making room. Whether there is more to note: a window was dealt with, or measured."""
    blocked = again = False
    for p in list(st.pending):
        boot = p["boot"]
        forced = bool(going & _segs_of(cat, boot, p["lo"], p["hi"]))
        every = held.every
        if any(e.id == _id(boot, p["seq"]) for e in every):
            st.pending.remove(p)  # copied, and a crash before that was noted
            again = True
            continue
        used = sum(e.size for e in every)
        if not forced and "size" in p and (blocked or used + p["size"] > limit):
            blocked = True
            continue
        again = True
        rows = _crossings(cat, boot, p["lo"], p["hi"])
        if rows and all(r["verdict"] == 0 for r in rows):
            st.pending.remove(p)  # judged since, and none
            continue
        seqs = [r["seq"] for r in rows if r["verdict"] != 0] or [p["seq"]]
        room = limit // _BOOT - sum(e.size for e in every if e.boot == boot)
        most = min(limit // _WINDOW, room)
        new = root() / ".new" / _id(boot, p["seq"])
        e = _copy(cat, p, rows, new, most) if room >= limit // _LEAST else None
        if e is None:
            st.pending.remove(p)
            if room >= limit // _LEAST:
                _lost(st, p, seqs)
            else:
                st.count(boot, seqs)
            continue
        size = _size(new) + _reserved(e)
        if size > most:
            shutil.rmtree(new, ignore_errors=True)
            st.pending.remove(p)
            st.count(boot, seqs)
            st.tell(
                f"the crossing at {_at(p['time'])} is too large for a window's share of the cap "
                f"({size} bytes; `evidence_mb`): counted, not kept"
            )
            continue
        if not forced and (blocked or used + size > limit):
            p["size"], blocked = size, True
            shutil.rmtree(new, ignore_errors=True)
            continue
        if used + size > limit:  # noted before the new one is there: a crash between is finished
            st.making = boot
            st.save()
        shutil.rmtree(e.path, ignore_errors=True)  # one whose meta.json can't be read
        e.size = _size(new)
        if _waits(e):  # noted before it is there, or a crash would leave it waiting for good
            st.unsettled = [*(st.unsettled or []), e.id]
            st.save()
        new.rename(e.path)
        held.put(e)
        st.pending.remove(p)
        if any(e.hi < q for q in seqs):  # cut short of crossings taken to be in it
            st.done[boot] = e.hi
        _finish(st, held, limit)
    if blocked and not st.full:
        st.tell(
            f"the evidence of crossings is full ({limit >> 20} MB): a new one waits in the store, "
            "and replaces the oldest when its records would be removed — `fy monitor evidence`",
            notify=True,
        )
    st.full = blocked
    return again


def _finish(st: State, held: Held, limit: int) -> None:
    """Make the room a new window has been published without (``st.making``): only once it is
    there, and after a crash between the two. Never for a cap merely lowered."""
    if not st.making:
        return
    used = sum(e.size for e in held.every)
    for old in _victims(held.every, st.making):
        if used <= limit:
            break
        _evict(st, old)
        held.drop(old)
        used -= old.size
    st.making = ""


def _evict(st: State, old: Evidence) -> None:
    shutil.rmtree(old.path, ignore_errors=True)
    st.tell(
        f"the evidence of the crossing at {_at(old.time)} made way for a new boot's: the cap was "
        "full, and the new one's records were about to be removed",
        notify=True,
    )


def _lost(st: State, p: dict, seqs: list[int]) -> None:
    st.tell(
        f"nothing is left of the records around the crossing at {_at(p['time'])} "
        f"({len(seqs)} in it)"
    )


def _victims(every: list[Evidence], boot: str) -> list[Evidence]:
    """What makes way for ``boot``'s new window, in order: the oldest boots' later windows, the
    latest of a boot first; then their first ones. Never ``boot``'s own."""
    boots: dict[str, list[Evidence]] = {}
    for e in every:
        if e.boot != boot:
            boots.setdefault(e.boot, []).append(e)
    later = [e for es in boots.values() for e in reversed(es[1:])]
    return later + [es[0] for es in boots.values()]


def _crossings(cat: monitorstore.Catalogue, boot: str, lo: int, hi: int) -> list[dict]:
    return [
        {"seq": int(seq), "time": when, "verdict": verdict, "container": cid}
        for seq, when, verdict, cid in cat.conn.execute(
            "SELECT seq, time, verdict, container FROM crossing WHERE boot = ? AND seq BETWEEN "
            "? AND ? ORDER BY seq",
            (boot, lo, hi),
        )
    ]


def _copy(
    cat: monitorstore.Catalogue, p: dict, rows: list[dict], new: Path, most: int
) -> Evidence | None:
    """Write window ``p`` to ``new``, all of it within ``most`` bytes when that can be done (its
    crossing alone may be more); ``None`` when the store has none of its records left."""
    boot, seq, lo, hi = p["boot"], int(p["seq"]), int(p["lo"]), int(p["hi"])
    held = _segs_of(cat, boot, lo, hi)
    segs = [s for s in monitorstore.segments() if s.id in held]
    sizes = [(int(r["seq"]), len(line)) for line, r in _lines(segs, boot)
             if "seq" in r and lo <= int(r["seq"]) <= hi]  # fmt: skip
    if not sizes:
        return None
    for d in (root(), new.parent):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    new.mkdir(mode=0o700)
    raw, e = most * _RATIO, None
    listed = [r for r in rows if r["verdict"] != 0]
    for left in range(5, -1, -1):  # compression is the content's: fewer records until they fit
        first, last = _fit(sizes, seq, raw if left else 0)
        events, inodes, snaps = _write_records(new, segs, boot, first, last)
        _write_context(cat, new, boot, first, last, inodes, snaps)
        inside = [r for r in listed if first <= r["seq"] <= last]
        e = Evidence(
            id=_id(boot, seq), boot=boot, seq=seq, time=p["time"], lo=first, hi=last,
            events=events, recv=p["recv"], open=cat.after(boot, last) is None,
            unjudged=[r["seq"] for r in inside if r["verdict"] is None],
            missing=_missing(cat, p, sizes, (first, last)),
        )  # fmt: skip
        _list(e, [r for r in inside if r["verdict"] == 1])
        _write(new / "meta.json", _meta(e))
        size = _size(new) + _reserved(e)
        if size <= most:
            break
        raw = min(raw, sum(n for s, n in sizes if first <= s <= last)) * most // size * 9 // 10
    return e


def _reserved(e: Evidence) -> int:
    """What naming the crossings among those not judged yet would add to its meta.json."""
    return min(len(e.unjudged), _LISTED) * _LISTING + 2 * len(_NO_ROOM) * e.open


def _write_context(
    cat: monitorstore.Catalogue,
    new: Path,
    boot: str,
    first: int,
    last: int,
    inodes: set[str],
    snaps: set[str],
) -> None:
    near = monitorlog.around(cat, boot, first, last, inodes)
    for r in near:
        if r.get("k") == "scopes":
            snaps |= {cid for ids in r["map"].values() for cid in ids}
    claims = {cid: meta for cid, meta in cat.claims().items() if cid in snaps}
    with _gz(new / "context.jsonl.gz") as f:
        for r in [*near, {"k": "engine", "containers": claims}]:
            f.write(monitorstore._dump(r))


def _fit(sizes: list[tuple[int, int]], seq: int, raw: int) -> tuple[int, int]:
    """The run of sequence numbers around ``seq`` whose lines fit in ``raw`` bytes: half for each
    side, outwards from it. Its own line is always in."""
    own = sum(n for s, n in sizes if s == seq)
    ends = []
    for side in (sorted((x for x in sizes if x[0] < seq), reverse=True),
                 sorted(x for x in sizes if x[0] > seq)):  # fmt: skip
        left, end = (raw - own) // 2, seq
        for s, n in side:
            left -= n
            if left < 0:
                break
            end = s
        ends.append(end)
    return ends[0], ends[1]


@contextmanager
def _gz(path: Path) -> Iterator[gzip.GzipFile]:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as out:
            yield out


def _write_records(
    new: Path, segs: list[monitorstore.Segment], boot: str, first: int, last: int
) -> tuple[int, set[str], set[str]]:
    """Write ``boot``'s lines from ``first`` to ``last``, and what has no number of its own (a
    transport gap, a line that failed its signature) between them: the events written, the mount
    namespaces they touch and the containers their snapshots name."""
    events, inodes, named, inside = 0, set(), set(), False
    with _gz(new / "records.jsonl.gz") as f:
        for line, rec in _lines(segs, boot):
            if "seq" in rec:
                inside = first <= int(rec["seq"]) <= last
            elif "from" in rec:
                inside = first <= int(rec["from"]) <= last
            if not inside:
                continue
            f.write(line)
            if rec.get("k") == "ev":
                events += 1
                inodes |= monitorlog.touched(rec)
            elif rec.get("k") == "scopes":
                named |= {cid for ids in rec["map"].values() for cid in ids}
    return events, inodes, named


def _missing(
    cat: monitorstore.Catalogue, p: dict, sizes: list[tuple[int, int]], got: tuple[int, int]
) -> list[str]:
    out = []
    seqs = {s for s, _ in sizes}
    for n, side in ((len([s for s in seqs if s < got[0]]), "before"),
                    (len([s for s in seqs if s > got[1]]), "after")):  # fmt: skip
        if n:
            out.append(f"{n} records at its {side} end, past a window's share of the cap")
    (oldest,) = cat.conn.execute("SELECT MIN(lo) FROM span WHERE boot = ?", (p["boot"],)).fetchone()
    if oldest is not None and oldest > 0 and oldest >= got[0] and p["floor"] < oldest - 1:
        out.append("records at its start the store had already removed")
    if p["cut"]:
        out.append("the end: the store needed its records' room before they had all arrived")
    return out


def _meta(e: Evidence) -> bytes:
    return json.dumps({k: v for k, v in asdict(e).items() if k in _FIELDS}, indent=1).encode()


# ── telling and reading it ────────────────────────────────────────────────────────────


def tell(log: Callable[[str], None], notify: Callable[[str, str], None] | None = None) -> None:
    """Say what :func:`keep` left to say (the pull's loop, after each pull)."""
    st = State.load()
    if not st.say:
        return
    said, st.say = st.say, []
    st.save()
    for s in said:
        log(f"⚠ guest monitor: {s['text']}")
        if s.get("notify") and notify is not None:
            notify(f"fy {config.project()}: evidence of a crossing", s["text"])


def note() -> str:
    """What is wrong with the evidence, for `fy monitor` and doctor; "" when nothing is."""
    if not cap():
        return ""
    st = State.load()
    if st.error:
        return (
            f"what is around a crossing can't be copied out of the store ({_safe(st.error)}): its "
            "retention is held back meanwhile, as far as the disk has room"
        )
    if st.full and st.pending:
        return (
            f"the evidence of crossings is full: {len(st.pending)} more wait in the store, and "
            "replace the oldest when their records would be removed — `fy monitor evidence "
            "<id> --remove` makes room"
        )
    return cap_setting()[1]


def summary() -> str:
    every = kept()
    if not every:
        return ""
    used = sum(e.size for e in every) / 2**20
    return f"evidence of {len(every)} crossings kept: {used:.1f} of {cap() >> 20} MB"


def read(e: Evidence) -> Iterator[monitorlog.Event]:
    """``e``'s events, joined by its own records and context alone — a chunk at a time (a storm's
    window is tens of thousands)."""
    records = e.path / "records.jsonl.gz"
    near = list(_jsonl(e.path / "context.jsonl.gz"))
    near += [r for r in _jsonl(records) if r.get("k") == "scopes"]
    prefix = config.project_prefix()
    chunk: list[dict] = []
    for rec in _jsonl(records):
        if rec.get("k") == "ev":
            chunk.append(rec)
        if len(chunk) >= monitorlog.CHUNK:
            yield from monitorlog.attribute([*chunk, *near], prefix)
            chunk = []
    yield from monitorlog.attribute([*chunk, *near], prefix)


def _at(when: str) -> str:
    return f"{_safe(when[:19]).replace('T', ' ')} UTC"


def _safe(text: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in text)


def _row(e: Evidence) -> str:
    cids = list(dict.fromkeys(c["container"][:12] for c in e.crossings if c.get("container")))
    n = len(e.crossings) + e.more + len(e.unjudged)
    missing = list(e.missing)
    if e.open and _NO_ROOM not in missing:
        missing.append("a snapshot after it: its last events may be unnamed")
    return (
        f"{_safe(e.id)}\n    {_at(e.time)}, from {_safe(', '.join(cids[:3])) or '?'}"
        f"{'' if e.confirmed else ' (unconfirmed)'}: {n} crossing{'' if n == 1 else 's'}, "
        f"{e.events} events, {e.size / 2**20:.1f} MB"
        + "".join(f"\n    ⚠ missing: {_safe(m)}" for m in missing)
    )


def _find(ident: str) -> Evidence | None:
    every = kept()
    exact = [e for e in every if e.id == ident]
    found = exact or [e for e in every if e.id.startswith(ident)]
    return found[0] if len(found) == 1 else None


def show(ident: str = "", as_json: bool = False) -> int:
    """`fy monitor evidence [ID]`: what is kept, or one piece of it."""
    if not ident:
        every = kept()
        for e in every:
            print(json.dumps(asdict(e)) if as_json else _row(e))
        if as_json:
            return 0
        if not every:
            print("no evidence kept — nothing has crossed out of a container that the store held")
        st = State.load()
        for boot, n in sorted(st.past.items()):
            print(f"⚠ boot {_safe(boot)[:8]}: {n} later crossings past its share, not kept")
        why = note()
        if why:
            print(f"⚠ {why}")
        return 0
    e = _find(ident)
    if e is None:
        print(f"✗ no one piece of evidence is called {_safe(ident)} — `fy monitor evidence`")
        return 1
    for ev in read(e):
        if as_json:
            print(json.dumps(asdict(ev)))
        else:
            what = _safe(ev.detail or ev.args)
            stamp = _safe(ev.time[:19]).replace("T", " ")
            who = _safe(monitorlog._who(ev))
            print(f"{stamp}  {who:<28.28}  {ev.kind:<7}  {_safe(ev.binary):<28.28}  {what[:80]}")
    if not as_json:
        print(f"— {_row(e)}")
        print(f"— the records themselves: {e.path}")
    return 0


def remove(ident: str) -> int:
    e = _find(ident)
    if e is None:
        print(f"✗ no one piece of evidence is called {_safe(ident)} — `fy monitor evidence`")
        return 1
    shutil.rmtree(e.path)
    stamp = datetime.now(UTC).strftime("%H:%M")
    print(f"✓ removed {_safe(e.id)} ({stamp} UTC); a window waiting for room is kept next")
    return 0
