"""monitorevidence.py — the records around a step out of a container, kept beside the guest
monitor's store and past its budget (docs/monitor-pipeline.md §3.3).

Once a crossing's segment leaves the store, a time and a container are all that remain of it. So
each one's WINDOW — its boot's records from :data:`SIDE_SECONDS` before it to as long after — is
copied to ``logs/evidence/<id>/``, which no reader of the store lists (sparing the segments in place
would leave holes in a store whose hooks and retention take it to be contiguous):

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
    crossings: list[dict] = field(default_factory=list)  # {seq, time, verdict, container}
    more: int = 0  # crossings in it past those listed
    missing: list[str] = field(default_factory=list)
    size: int = 0  # on disk; measured, never stored

    @property
    def path(self) -> Path:
        return root() / self.id

    @property
    def confirmed(self) -> bool:
        return any(c.get("verdict") == 1 for c in self.crossings)


_FIELDS = frozenset(Evidence.__dataclass_fields__) - {"id", "size"}


def _size(path: Path) -> int:
    total = 0
    for f in path.iterdir():
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def kept() -> list[Evidence]:
    """Every piece of evidence, oldest boot first, then in sequence."""
    try:
        names = sorted(os.listdir(root()))
    except OSError:
        return []
    out = []
    for name in names:
        path = root() / name
        if name.startswith(".") or not path.is_dir():
            continue
        try:
            doc = json.loads((path / "meta.json").read_text())
            known = {k: v for k, v in doc.items() if k in _FIELDS}
            out.append(Evidence(id=name, **known, size=_size(path)))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    first: dict[str, float] = {}
    for e in out:
        first[e.boot] = min(first.get(e.boot, float("inf")), e.recv)
    return sorted(out, key=lambda e: (first[e.boot], e.boot, e.seq))


@dataclass
class State:
    done: dict[str, int] = field(default_factory=dict)  # boot -> the last crossing dealt with
    past: dict[str, int] = field(default_factory=dict)  # boot -> crossings past its share
    pending: list[dict] = field(default_factory=list)  # windows waiting for room
    say: list[dict] = field(default_factory=list)  # for the supervisor to log ({text, notify})
    full: bool = False  # said to be full
    error: str = ""

    @classmethod
    def load(cls) -> State:
        try:
            doc = json.loads(_state_file().read_text())
            return cls(**{k: v for k, v in doc.items() if k in cls.__dataclass_fields__})
        except (OSError, ValueError, TypeError, AttributeError):
            return cls()

    def save(self) -> None:
        _write(_state_file(), json.dumps(asdict(self)).encode())

    def tell(self, text: str, notify: bool = False) -> None:
        self.say = [*self.say, {"text": text, "notify": notify}][-50:]


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    before = asdict(st)
    try:
        for stale in root().glob(".new-*"):  # a crash while writing one
            shutil.rmtree(stale, ignore_errors=True)
        _confirm(cat, st)
        for _ in range(_BOOT * _WINDOW):  # a window cut short leaves crossings to note again
            for boot in sorted(boots, key=lambda b: _age(cat, b)):
                _note(cat, st, boot, now, limit, going)
            if not _flush(cat, st, limit, going):
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


def _confirm(cat: monitorstore.Catalogue, st: State) -> None:
    """Take the verdicts judged since on what was kept unconfirmed: evidence that turns out to
    hold no crossing goes."""
    for e in kept():
        if all(c.get("verdict") is not None for c in e.crossings):
            continue
        changed = False
        for c in e.crossings:
            row = cat.conn.execute(
                "SELECT verdict, container FROM crossing WHERE boot = ? AND seq = ?",
                (e.boot, c["seq"]),
            ).fetchone()
            if c.get("verdict") is None and row is not None and row[0] is not None:
                c["verdict"], c["container"], changed = int(row[0]), row[1], True
        if not changed:
            continue
        if not e.more and all(c.get("verdict") == 0 for c in e.crossings):
            shutil.rmtree(e.path, ignore_errors=True)
            st.tell(f"the evidence kept at {_at(e.time)} was removed: judged not to be a crossing")
        else:
            _write(e.path / "meta.json", _meta(e))


def _note(
    cat: monitorstore.Catalogue,
    st: State,
    boot: str,
    now: float,
    limit: int,
    going: frozenset[str],
) -> None:
    """Make a pending window of each of ``boot``'s crossings not yet in one, in sequence, up to the
    first whose window isn't whole."""
    mine = [e for e in kept() if e.boot == boot]
    done = st.done.get(boot)
    if done is None:  # the state was lost: nothing kept is kept again
        done = max((e.hi for e in mine), default=-1)
    ended = _ended(cat, boot)
    rows = cat.conn.execute(
        # one not judged by then is kept unconfirmed
        "SELECT seq, time FROM crossing WHERE boot = ? AND seq > ? AND "
        "(verdict IS NULL OR verdict = 1) ORDER BY seq",
        (boot, done),
    ).fetchall()
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
        held = sum(e.size for e in mine) + sum(p.get("size", limit // _WINDOW) for p in waiting)
        if limit // _BOOT - held < limit // _LEAST:
            st.past[boot] = st.past.get(boot, 0) + 1
            st.done[boot] = seq
            continue
        floor = max((hi for _, hi in spans if hi < seq), default=-1)
        lo, hi = _window(cat, boot, seq, t, floor)
        st.pending.append({
            "boot": boot, "seq": seq, "time": when, "lo": lo, "hi": hi, "recv": now,
            "cut": not whole, "floor": floor,
        })  # fmt: skip
        st.done[boot] = seq


def _segs_from(cat: monitorstore.Catalogue, boot: str, seq: int, t: int | None) -> set[str]:
    """The segments holding what a window at ``seq`` starts with."""
    out = set()
    for sid, t1 in cat.conn.execute(
        "SELECT span.seg, seg.t1 FROM span JOIN seg ON seg.id = span.seg "
        "WHERE span.boot = ? AND span.lo <= ?",
        (boot, seq),
    ):
        end = monitorstore.stamp_ns(t1)
        if t is None or end is None or end >= t - SIDE_SECONDS * 10**9:
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
    side = SIDE_SECONDS * 10**9
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


def _flush(cat: monitorstore.Catalogue, st: State, limit: int, going: frozenset[str]) -> bool:
    """Copy the pending windows there is room for, oldest first — and those ``going`` would take
    from, making room. Whether one was cut short of crossings taken to be in it: they are past it
    now, to note again."""
    blocked = again = False
    for p in list(st.pending):
        forced = bool(going & _segs_of(cat, p["boot"], p["lo"], p["hi"]))
        every = kept()
        if any(e.id == _id(p["boot"], p["seq"]) for e in every):
            st.pending.remove(p)  # copied, and a crash before that was noted
            continue
        used = sum(e.size for e in every)
        if not forced and (blocked or used + p.get("size", 0) > limit):
            blocked = True
            continue
        rows = _crossings(cat, p["boot"], p["lo"], p["hi"])
        if rows and all(r["verdict"] == 0 for r in rows):
            st.pending.remove(p)  # judged since, and none
            continue
        room = limit // _BOOT - sum(e.size for e in every if e.boot == p["boot"])
        if room < limit // _LEAST:
            st.past[p["boot"]] = st.past.get(p["boot"], 0) + 1
            st.pending.remove(p)
            continue
        new = root() / f".new-{_id(p['boot'], p['seq'])}"
        e = _copy(cat, p, rows, new, min(limit // _WINDOW, room))
        if e is None:
            st.pending.remove(p)
            st.tell(f"nothing is left of the records around the crossing at {_at(p['time'])}")
            continue
        size = _size(new)
        if used + size > limit and not forced:
            p["size"], blocked = size, True
            shutil.rmtree(new, ignore_errors=True)
            continue
        shutil.rmtree(e.path, ignore_errors=True)  # one whose meta.json can't be read
        new.rename(e.path)
        st.pending.remove(p)
        if any(e.hi < r["seq"] for r in rows):
            st.done[p["boot"]], again = e.hi, True
        used += size
        for old in _victims(every, p["boot"]):  # only now the new one is there
            if used <= limit:
                break
            shutil.rmtree(old.path, ignore_errors=True)
            used -= old.size
            st.tell(
                f"the evidence of the crossing at {_at(old.time)} made way for the one at "
                f"{_at(p['time'])}: the cap was full, and its records were about to be removed",
                notify=True,
            )
    if blocked and not st.full:
        st.tell(
            f"the evidence of crossings is full ({limit >> 20} MB): a new one waits in the store, "
            "and replaces the oldest when its records would be removed — `fy monitor evidence`",
            notify=True,
        )
    st.full = blocked
    return again


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
    """Write window ``p`` to ``new``, its records within ``most`` compressed bytes; ``None`` when
    the store has none of them left."""
    boot, seq, lo, hi = p["boot"], int(p["seq"]), int(p["lo"]), int(p["hi"])
    held = _segs_of(cat, boot, lo, hi)
    segs = [s for s in monitorstore.segments() if s.id in held]
    sizes = [(int(r["seq"]), len(line)) for line, r in _lines(segs, boot)
             if "seq" in r and lo <= int(r["seq"]) <= hi]  # fmt: skip
    if not sizes:
        return None
    root().mkdir(mode=0o700, parents=True, exist_ok=True)
    new.mkdir(mode=0o700)
    raw = most * _RATIO
    first, last, events, inodes, snaps = lo, hi, 0, set(), set()
    for _ in range(6):  # compression is the content's: fewer records until they fit
        first, last = _fit(sizes, seq, raw)
        events, inodes, snaps = _write_records(new, segs, boot, first, last)
        size = (new / "records.jsonl.gz").stat().st_size
        if size <= most:
            break
        raw = min(raw, sum(n for s, n in sizes if first <= s <= last)) * most // size * 9 // 10
    near = monitorlog.around(cat, boot, first, last, inodes)
    for r in near:
        if r.get("k") == "scopes":
            snaps |= {cid for ids in r["map"].values() for cid in ids}
    claims = {cid: meta for cid, meta in cat.claims().items() if cid in snaps}
    with _gz(new / "context.jsonl.gz") as f:
        for r in [*near, {"k": "engine", "containers": claims}]:
            f.write(monitorstore._dump(r))
    listed = [r for r in rows if r["verdict"] != 0 and first <= r["seq"] <= last]
    e = Evidence(
        id=_id(boot, seq), boot=boot, seq=seq, time=p["time"], lo=first, hi=last, events=events,
        recv=p["recv"], crossings=listed[:_LISTED], more=len(listed[_LISTED:]),
        missing=_missing(cat, p, sizes, (first, last)),
    )  # fmt: skip
    _write(new / "meta.json", _meta(e))
    return e


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
    if cat.after(p["boot"], got[1]) is None:
        out.append("a snapshot after it: the last containers in it may be unnamed")
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
            f"what is around a crossing can't be copied out of the store ({st.error}): its "
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

    def lines(name: str) -> Iterator[dict]:
        with gzip.open(e.path / name, "rb") as f:
            for line in f:
                yield json.loads(line)

    near = list(lines("context.jsonl.gz"))
    near += [r for r in lines("records.jsonl.gz") if r.get("k") == "scopes"]
    prefix = config.project_prefix()
    chunk: list[dict] = []
    for rec in lines("records.jsonl.gz"):
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
    n = len(e.crossings) + e.more
    return (
        f"{_safe(e.id)}\n    {_at(e.time)}, from {_safe(', '.join(cids[:3])) or '?'}"
        f"{'' if e.confirmed else ' (unconfirmed)'}: {n} crossing{'' if n == 1 else 's'}, "
        f"{e.events} events, {e.size / 2**20:.1f} MB"
        + "".join(f"\n    ⚠ missing: {_safe(m)}" for m in e.missing)
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
