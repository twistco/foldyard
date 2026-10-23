"""monitorstore.py — the guest monitor's store on your computer: segments named by their first
line, sealed and compressed with positions intact, retention by a host-only disk budget, and the
catalogue that lets any stretch of the store be joined as a whole. All in a tmp state dir."""

from __future__ import annotations

import gzip
import json

import pytest

from foldyard import monitorstore as ms

BOOT = "boot-1"
BOX = "a" * 64


def ev(seq: int, inode: int = 200, boot: str = BOOT) -> dict:
    proc = {"binary": "/bin/cat", "pid": 7, "uid": 1, "ns": {"mnt": {"inum": inode}}}
    return {"k": "ev", "boot": boot, "seq": seq, "recv": 1.0,
            "e": {"process_exec": {"process": proc}, "time": f"2026-10-21T20:00:{seq:02d}Z"}}  # fmt: skip


def snap(seq: int, boot: str = BOOT, mapping=None) -> dict:
    return {"k": "scopes", "boot": boot, "seq": seq, "vm": 1,
            "map": {"200": [BOX]} if mapping is None else mapping}  # fmt: skip


@pytest.fixture(autouse=True)
def state(monkeypatch, tmp_path):
    monkeypatch.setattr(ms.config, "state_dir", lambda: tmp_path)
    return tmp_path


def _seq(rec: dict | None) -> int:
    assert rec is not None
    return rec["seq"]


def _seqs(read) -> list[int]:
    return [r["seq"] for r, _ in read if "seq" in r]


def test_a_live_segment_is_named_by_its_first_line(state):
    ms.append([snap(0), ev(1)])
    (seg,) = ms.segments()
    head = json.loads((state / "logs" / "monitor.jsonl").read_text().splitlines()[0])
    assert seg.live and head == {"k": "segment", "id": seg.id, "t": head["t"]}
    read, found = ms.read(None, 1 << 20)
    assert found and _seqs(read) == [0, 1]  # the first line is the store's, never a record


def test_a_sealed_segment_is_compressed_and_positions_survive_both(state, monkeypatch):
    ms.append([snap(0), ev(1), ev(2)])
    read, _ = ms.read(None, 1 << 20)
    at = read[1][1]  # just past ev 1, in the live segment
    monkeypatch.setattr(ms, "SEGMENT_BYTES", 10)
    ms.append([ev(3)])  # seals the full live segment first
    sealed, live = ms.segments()
    assert sealed.gz and sealed.id == at[0] and live.live and live.id != at[0]
    assert not (state / "logs" / f"monitor.{sealed.id}.jsonl").exists()  # only compressed
    read, found = ms.read(at, 1 << 20)
    assert found and _seqs(read) == [2, 3]  # the same position, now inside a .gz
    assert ms.find([BOOT, 1]) == at  # and found again by the event alone
    assert ms.raw_size(sealed) == len(gzip.decompress(sealed.path.read_bytes()))


def test_the_budget_removes_the_oldest_sealed_segments_never_the_live(state, monkeypatch):
    monkeypatch.setattr(ms, "SEGMENT_BYTES", 10)
    monkeypatch.setattr(ms, "budget", lambda: 1)
    for i in range(0, 9, 3):
        ms.append([snap(i), ev(i + 1), ev(i + 2)])
    (live,) = ms.segments()
    assert live.live and _seqs(ms.read(None, 1 << 20)[0]) == [6, 7, 8]
    with ms.Catalogue.open() as cat:
        # the segments' snapshots went with them — but the newest of the boot's stays: the one
        # before the oldest events still stored
        assert _seq(cat.before(BOOT, 6)) == 3
        assert cat.before(BOOT, 3) is None


def test_the_default_budget_is_8_gb(state):
    # A busy project writes 21–61 MB an hour; its full 1 GB store reached back 15–35 h (2026-10-23).
    assert not ms.settings_file().exists()
    assert ms.budget_setting() == (8 << 30, "")


def test_the_budget_is_host_only_and_clamped(state):
    assert ms.settings_file() == state / "monitor-store.toml"  # the state dir, not the checkout
    assert ms.budget_setting() == (ms.BUDGET_MB << 20, "")
    (state / "monitor-store.toml").write_text("max_mb = 4096\n")
    assert ms.budget_setting() == (4096 << 20, "")
    (state / "monitor-store.toml").write_text("max_mb = 1\n")
    size, note = ms.budget_setting()
    assert size == ms.BUDGET_MIN_MB << 20 and "outside" in note
    (state / "monitor-store.toml").write_text('max_mb = "lots"\n')
    assert ms.budget_setting()[0] == ms.BUDGET_MB << 20
    (state / "monitor-store.toml").write_text("max_mb = [\n")
    assert "unreadable" in ms.budget_setting()[1]


def test_an_older_foldyards_store_is_indexed_sealed_and_compressed(state):
    logs = state / "logs"
    logs.mkdir()
    lines = lambda *rs: "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rs)  # noqa: E731
    (logs / "monitor.20261001T000000Z.jsonl").write_text(lines(snap(0), ev(1)))
    (logs / "monitor.jsonl").write_text(lines(ev(2), snap(3, mapping={})))  # no first line
    old, live = ms.segments()
    assert old.id == "20261001T000000Z" and live.id.startswith(ms.LEGACY)
    with ms.Catalogue.open() as cat:
        cat.catch_up()
        assert _seq(cat.before(BOOT, 2)) == 0 and _seq(cat.after(BOOT, 1)) == 3
    ms.append([ev(4)])  # the older live file is sealed under an id of its own; both compressed
    ids = [s.id for s in ms.segments()]
    assert ids[0] == "20261001T000000Z" and len(ids) == 3
    assert not any(i.startswith(ms.LEGACY) for i in ids)
    assert all(s.gz for s in ms.segments()[:-1])
    assert _seqs(ms.read(None, 1 << 20)[0]) == [0, 1, 2, 3, 4]
    with ms.Catalogue.open() as cat:
        assert cat.last_snapshot(BOOT) == 3


def test_a_lost_catalogue_is_rebuilt_from_the_segments(state, monkeypatch):
    monkeypatch.setattr(ms, "SEGMENT_BYTES", 10)
    ms.append([snap(0), ev(1)])
    ms.append([{"k": "engine", "containers": {BOX: {"name": "fyex-devbox"}}}, snap(2)])
    for p in (state / "logs").glob("monitor-index.sqlite*"):
        p.unlink()
    (state / "logs" / "monitor-index.sqlite").write_text("not a database")
    with ms.Catalogue.open() as cat:
        cat.catch_up()
        assert _seq(cat.after(BOOT, 1)) == 2
        assert cat.claims() == {BOX: {"name": "fyex-devbox"}}


def test_records_the_catalogue_missed_are_indexed_before_the_store_moves_on(state):
    ms.append([snap(0)])
    (live,) = ms.segments()
    with (state / "logs" / "monitor.jsonl").open("a") as f:  # written, never indexed (a crash)
        f.write(json.dumps(snap(1)) + "\n")
    ms.append([ev(2)])
    with ms.Catalogue.open() as cat:
        assert cat.last_snapshot(BOOT) == 1
    with ms.Catalogue.open() as cat:  # and indexed past the end of a write that never happened
        cat.add(live.id, [], indexed=10_000, exact=True)
    ms.append([snap(3)])
    with ms.Catalogue.open() as cat:
        assert cat.last_snapshot(BOOT) == 3


def test_a_read_never_stops_short_of_an_event(state):
    big = {"k": "engine", "containers": {f"{i:064d}": {"name": "x" * 200} for i in range(20)}}
    ms.append([snap(0), big, ev(1)])
    read, _ = ms.read(None, 10)
    assert [r["k"] for r, _ in read] == ["scopes", "engine", "ev"]


def test_tail_is_a_position_that_many_bytes_before_the_end(state, monkeypatch):
    monkeypatch.setattr(ms, "SEGMENT_BYTES", 10)
    ms.append([snap(0), ev(1)])
    ms.append([ev(2), ev(3)])
    assert ms.tail(0) == [ms.segments()[-1].id, ms.raw_size(ms.segments()[-1])]
    assert _seqs(ms.read(ms.tail(1 << 20), 1 << 20)[0]) == [0, 1, 2, 3]
    assert ms.tail(1, []) is None


def test_an_older_supervisor_rotating_under_a_newer_reader_leaves_nothing_unindexed(state):
    # the transition: the old supervisor still writes headerless files and rotates them by its
    # own naming, while a newer `fy monitor` indexes what it finds
    logs = state / "logs"
    logs.mkdir()
    write = lambda p, *rs: p.write_text(  # noqa: E731
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rs)
    )
    write(logs / "monitor.jsonl", snap(0), ev(1), snap(2), ev(3), ev(4))
    with ms.Catalogue.open() as cat:
        cat.catch_up()
    (logs / "monitor.jsonl").rename(logs / "monitor.20261002T000000Z.jsonl")  # the old rotation
    write(logs / "monitor.jsonl", ev(5), snap(6, mapping={}))  # a new, shorter, headerless file
    with ms.Catalogue.open() as cat:
        cat.catch_up()
        assert cat.last_snapshot(BOOT) == 6  # the new file wasn't taken for the old one
        assert _seq(cat.before(BOOT, 5)) == 2 and _seq(cat.before(BOOT, 1)) == 0  # both kept
        assert not any(
            sid.startswith(ms.LEGACY) for sid in cat.bounds() if sid != ms.segments()[-1].id
        )


def test_a_locked_catalogue_is_done_without_never_wiped(state, monkeypatch):
    ms.append([snap(0), ev(1)])
    marker = ms.index_path().stat().st_ino

    def locked(cls, path):
        raise ms.sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ms.Catalogue, "_connect", classmethod(locked))
    assert ms.Catalogue.open_or_none() is None
    assert ms.index_path().stat().st_ino == marker  # still the same file


def test_a_segment_sealed_under_a_readers_listing_isnt_forgotten(state, monkeypatch):
    # the listing caught the directory before the seal's rename and the new live file after:
    # the just-sealed segment is missing from it, and older than everything it shows
    ms.append([snap(0), ev(1)])
    (sealed,) = ms.segments()
    later = ms.Segment(sealed.id + "z", ms.live_path(), live=True)
    monkeypatch.setattr(ms, "segments", lambda: [later])
    with ms.Catalogue.open() as cat:
        cat.catch_up()
        assert sealed.id in cat.bounds() and _seq(cat.before(BOOT, 1)) == 0


def test_find_reads_a_segment_the_catalogue_hasnt_indexed_yet(state, monkeypatch):
    # a catalogue caught up with a listing from before a seal has no span for the sealed
    # segment: its silence about it is not "the store doesn't hold it"
    monkeypatch.setattr(ms, "SEGMENT_BYTES", 10)
    ms.append([snap(0), ev(1)])
    with ms.Catalogue.open() as cat:
        cat.conn.execute("DELETE FROM span")  # as if the segment had never been indexed
        cat.conn.execute("DELETE FROM seg")
        ms.append([ev(2)])  # sealed now, under the catalogue
        at = ms.find([BOOT, 1], ms.segments(), cat)
    assert at is not None and at[0] == ms.segments()[0].id


def test_an_older_foldyards_live_file_is_indexed_before_it_is_sealed(state, monkeypatch):
    # no reader has caught up since the upgrade: the seal itself must leave the catalogue
    # holding the segment under its new id — and its snapshot, when retention takes it at once
    logs = state / "logs"
    logs.mkdir()
    (logs / "monitor.jsonl").write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in (snap(0), ev(1), ev(2)))
    )
    ms.append([ev(3)])
    sealed = ms.segments()[0]
    with ms.Catalogue.open() as cat:
        assert cat.holding(BOOT, 1) == {sealed.id} and cat.unindexed([sealed]) == set()
    monkeypatch.setattr(ms, "budget", lambda: 1)  # keeps only the live segment
    (logs / "monitor.jsonl").rename(logs / "monitor.jsonl.x")
    (logs / "monitor.jsonl").write_text(json.dumps(snap(4)) + "\n" + json.dumps(ev(5)) + "\n")
    ms.append([ev(6)])
    with ms.Catalogue.open() as cat:
        assert _seq(cat.before(BOOT, 6)) == 4  # gone from the store, kept by the catalogue


def test_a_reader_racing_the_seal_of_an_older_live_file_leaves_no_row_for_it(state):
    # how cabash's catalogue got its empty `legacy-65215819` row (sealed 0, no span, no times)
    logs = state / "logs"
    logs.mkdir()
    (logs / "monitor.jsonl").write_text(json.dumps(snap(0)) + "\n" + json.dumps(ev(1)) + "\n")
    listing = ms.segments()  # the reader lists the store…
    ms.append([ev(2)])  # …the pull seals the older live file under an id of its own…
    with ms.Catalogue.open() as cat:
        cat.catch_up(listing)  # …and the reader catches up with what it listed
        assert not any(sid.startswith(ms.LEGACY) for sid in cat.bounds())


def test_a_row_for_a_segment_the_store_no_longer_has_is_dropped(state):
    # a reader racing the seal of an older foldyard's live file left an empty `legacy-<inode>`
    # row, which no reader ever dropped: each caught up with an explicit listing
    from foldyard import monitorlog

    ms.append([snap(0), ev(1)])
    with ms.Catalogue.open() as cat:
        cat.add(f"{ms.LEGACY}-65215819", [], 0)
    cat = monitorlog.catalogue()
    assert cat is not None
    try:
        assert not any(sid.startswith(ms.LEGACY) for sid in cat.bounds())
    finally:
        cat.close()


def init(
    seq: int, kind: str = "process_exec", inode: int = 200, start: str = "2026-10-21T20:00:00.5Z"
):
    """A container's init entering (exec) or leaving (exit) its namespace — a mark the join reads."""
    e = ev(seq, inode)
    body = e["e"].pop("process_exec")
    body["process"] |= {"in_init_tree": True, "start_time": start}
    body["parent"] = {"ns": {"mnt": {"inum": 7}}}
    e["e"][kind] = body
    return e


def _marks(cat, lo: int = -1, hi: int = 1 << 40) -> list[tuple]:
    return [(m["seq"], m["inode"], m["what"], m["start"]) for m in cat.marks(BOOT, lo, hi)]


def test_the_catalogue_keeps_each_containers_init_marks(state, monkeypatch):
    monkeypatch.setattr(ms, "SEGMENT_BYTES", 10)
    ms.append([snap(0), init(1), ev(2), init(3, "process_exit")])
    ms.append([snap(4), ev(5)])
    want = [
        (1, "200", "start", "2026-10-21T20:00:00.5Z"),
        (3, "200", "end", "2026-10-21T20:00:00.5Z"),
    ]
    with ms.Catalogue.open() as cat:
        assert _marks(cat) == want
        assert _marks(cat, 1, 4) == want[1:]  # strictly between the two seqs
    for p in (state / "logs").glob("monitor-index.sqlite*"):
        p.unlink()
    with ms.Catalogue.open() as cat:  # derived: a lost catalogue gets them back from the segments
        cat.catch_up()
        assert _marks(cat) == want


def test_a_process_entering_from_outside_but_not_an_init_is_no_mark(state):
    podman_exec = init(1)
    podman_exec["e"]["process_exec"]["process"]["in_init_tree"] = False  # conmon's exec session
    child = init(2)
    child["e"]["process_exec"]["parent"]["ns"]["mnt"]["inum"] = 200  # the init's own child
    ms.append([snap(0), podman_exec, child, init(3, "process_kprobe")])
    with ms.Catalogue.open() as cat:
        assert _marks(cat) == []


_V2_SQL = (
    "CREATE TABLE seg (id TEXT PRIMARY KEY, indexed INTEGER NOT NULL, sealed INTEGER NOT NULL, "
    "t0 TEXT NOT NULL, t1 TEXT NOT NULL)",
    "CREATE TABLE snap (boot TEXT NOT NULL, seq INTEGER NOT NULL, seg TEXT NOT NULL, "
    "rec TEXT NOT NULL, PRIMARY KEY (boot, seq))",
    "CREATE TABLE claim (cid TEXT PRIMARY KEY, meta TEXT NOT NULL, n INTEGER NOT NULL)",
    "CREATE TABLE span (seg TEXT NOT NULL, boot TEXT NOT NULL, lo INTEGER NOT NULL, "
    "hi INTEGER NOT NULL, PRIMARY KEY (seg, boot))",
)


def test_an_older_catalogue_gains_the_marks_and_keeps_what_it_cant_rebuild(state, monkeypatch):
    # 0.6.x's catalogue (schema 2), its segments all indexed and one sealed: the marks are read
    # from what it already indexed — and the snapshot it kept from a segment retention removed,
    # which nothing could rebuild, is still there
    import sqlite3

    monkeypatch.setattr(ms, "SEGMENT_BYTES", 10)
    ms.append([snap(10), init(11), ev(12)])
    ms.append([init(13, "process_exit"), snap(14)])
    sealed, live = ms.segments()
    path = state / "logs" / "monitor-index.sqlite"
    for p in (state / "logs").glob("monitor-index.sqlite*"):
        p.unlink()
    conn = sqlite3.connect(path, isolation_level=None)
    for statement in _V2_SQL:
        conn.execute(statement)
    conn.execute("INSERT INTO seg VALUES (?, ?, 1, '', '')", (sealed.id, ms.raw_size(sealed)))
    conn.execute("INSERT INTO seg VALUES (?, ?, 0, '', '')", (live.id, ms.raw_size(live)))
    orphan = snap(3)
    conn.execute("INSERT INTO snap VALUES (?, 3, 'gone', ?)", (BOOT, json.dumps(orphan)))
    for s in (snap(10), snap(14)):
        seg = sealed.id if s["seq"] == 10 else live.id
        conn.execute("INSERT INTO snap VALUES (?, ?, ?, ?)", (BOOT, s["seq"], seg, json.dumps(s)))
    conn.execute("PRAGMA user_version = 2")
    conn.close()
    with ms.Catalogue.open() as cat:
        cat.catch_up()
        assert [(m[0], m[2]) for m in _marks(cat)] == [(11, "start"), (13, "end")]
        assert _seq(cat.before(BOOT, 10)) == 3  # kept


def test_a_supervisor_still_on_0_6_works_beside_the_marks(state, monkeypatch):
    # an editable install upgrades the CLI at once and the supervisor only at its restart: the
    # marks must not make 0.6's catalogue code drop and rebuild the catalogue (a schema it doesn't
    # know) nor fail its writes (a column it doesn't know). Then 0.6 indexes a record without its
    # mark, and the next reader reads the mark from what 0.6 indexed.
    import sqlite3

    ms.append([snap(0), init(1)])
    path = state / "logs" / "monitor-index.sqlite"
    conn = sqlite3.connect(path, isolation_level=None)
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    cols = [row[1] for row in conn.execute("PRAGMA table_info(seg)")]
    assert version == 2 and cols == ["id", "indexed", "sealed", "t0", "t1"]
    (live,) = ms.segments()
    with (state / "logs" / "monitor.jsonl").open("a") as f:  # 0.6's pull: the record, then…
        f.write(json.dumps(init(2, "process_exit")) + "\n")
    conn.execute(  # …its catalogue write, exactly as 0.6 makes it
        "INSERT INTO seg VALUES (?, ?, 0, ?, ?) ON CONFLICT(id) DO UPDATE SET "
        "indexed = excluded.indexed",
        (live.id, ms.raw_size(live), "", ""),
    )
    conn.close()
    with ms.Catalogue.open() as cat:
        cat.catch_up()
        assert [(m[0], m[2]) for m in _marks(cat)] == [(1, "start"), (2, "end")]


def test_the_marks_move_with_a_sealed_segment_and_go_with_a_removed_one(state, monkeypatch):
    # an older foldyard's live file is sealed under a new id (`rename`): its marks and how far it
    # was read for them follow it, leaving no row on the old id; retention then removes it
    # (`drop`): its marks and that row go with it
    logs = state / "logs"
    logs.mkdir()
    (logs / "monitor.jsonl").write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in (snap(0), init(1), ev(2)))
    )
    (legacy,) = ms.segments()
    with ms.Catalogue.open() as cat:
        cat.catch_up()
    ms.append([ev(3)])
    sealed = ms.segments()[0]
    assert sealed.id != legacy.id
    with ms.Catalogue.open() as cat:
        assert _marks(cat) == [(1, "200", "start", "2026-10-21T20:00:00.5Z")]
        rows = cat.conn.execute("SELECT seg FROM mark").fetchall()
        read = dict(cat.conn.execute("SELECT id, marked FROM markseg").fetchall())
        (indexed,) = cat.conn.execute(
            "SELECT indexed FROM seg WHERE id = ?", (sealed.id,)
        ).fetchone()
        assert rows == [(sealed.id,)] and legacy.id not in read and read[sealed.id] == indexed
    monkeypatch.setattr(ms, "budget", lambda: 1)  # keeps only the live segment
    monkeypatch.setattr(ms, "SEGMENT_BYTES", 10)
    ms.append([init(4), ev(5)])  # seals the live segment, whose mark stays
    ms.append([ev(6)])  # and seals that one too: retention removes both sealed segments
    (live,) = ms.segments()
    with ms.Catalogue.open() as cat:
        assert _marks(cat) == []
        assert cat.conn.execute("SELECT id FROM markseg").fetchall() == [(live.id,)]


def test_a_mark_without_its_boot_or_seq_is_skipped_by_the_backfill(state):
    # a corrupted or hand-edited line in what 0.6 indexed: the backfill reads past it, as `add`
    # does, never failing every reader's catch-up on it
    bad = init(2)
    del bad["seq"]
    ms.append([snap(0), init(1), bad])
    with ms.Catalogue.open() as cat:
        cat.conn.execute("DELETE FROM mark")
        cat.conn.execute("DELETE FROM markseg")  # as 0.6 left it: indexed, never read for marks
        cat.catch_up()
        assert [m[0] for m in _marks(cat)] == [1]
