"""The records around a crossing, kept beside the guest monitor's store and past its budget
(docs/monitor-pipeline.md §3.3)."""

from __future__ import annotations

import json
import random
import stat
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from foldyard import cli, monitorlog
from foldyard import monitorevidence as me
from foldyard import monitorstore as ms

BOOT, BOX = "boot-1", "a" * 64
T0 = 1_790_000_000.0  # the guest's clock and your computer's agree here


def _time(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f000Z")


def _ns(stamp: str) -> int:
    ns = ms.stamp_ns(stamp)
    assert ns is not None
    return ns


@pytest.fixture(autouse=True)
def state(monkeypatch, tmp_path):
    monkeypatch.setattr(ms.config, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(ms.config, "project", lambda: "fyex")
    monkeypatch.setattr(ms.config, "project_prefix", lambda: "fyex")
    monkeypatch.setattr(ms.config, "in_box", lambda: False)
    return tmp_path


class Stream:
    """One boot after another, as the pull stores them: each record a guest time and the moment it
    reached your computer."""

    def __init__(self, boot: str = BOOT) -> None:
        self.boot, self.seq, self.t = boot, 0, 0.0

    def _rec(self, body: dict) -> dict:
        self.seq += 1
        return {**body, "boot": self.boot, "seq": self.seq - 1, "recv": T0 + self.t}

    def ev(self, inode: int = 200, parent: int | None = None, args: str = "", **proc) -> dict:
        body: dict = {"process": {"binary": "/bin/cat", "arguments": args, "pid": 7, "uid": 1,
                                  "ns": {"mnt": {"inum": inode}}, **proc}}  # fmt: skip
        if parent is not None:
            body["parent"] = {"binary": "/bin/sh", "ns": {"mnt": {"inum": parent}}}
        return self._rec({"k": "ev", "e": {"process_exec": body, "time": _time(T0 + self.t)}})

    def cross(self) -> dict:
        """A process in the VM's namespace whose parent was in the box's."""
        return self.ev(inode=1, parent=200)

    def snap(self, mapping: dict | None = None) -> dict:
        m = {"200": [BOX]} if mapping is None else mapping
        return self._rec({"k": "scopes", "t": T0 + self.t, "vm": 1, "map": m})

    def crossing(self, n: int = 1) -> list[dict]:
        """Two minutes either side of a crossing (``n`` of them), an event every 10 s and a
        snapshot every minute, until its window is whole."""
        out = [self.snap()]
        for i in range(13):
            out.append(self.ev(args=f"before {i}"))
            self.t += 10
        out += [self.cross() for _ in range(n)]
        for i in range(30):
            self.t += 10
            out.append(self.ev(args=f"after {i}"))
            if i % 6 == 0:
                out.append(self.snap())
        return out


def store(recs: list[dict], step: int = 5) -> None:
    for i in range(0, len(recs), step):
        ms.append(recs[i : i + step])


def caps(monkeypatch, n: int) -> None:
    monkeypatch.setattr(me, "cap", lambda: n)


def test_the_minute_either_side_of_a_crossing_is_kept_and_reads_by_itself(state):
    recs = Stream().crossing()
    ms.append([{"k": "engine", "containers": {BOX: {"name": "fyex-devbox"}}, "recv": T0}])
    store(recs)
    [e] = me.kept()
    assert e.confirmed and [c["container"] for c in e.crossings] == [BOX] and not e.missing
    truth = {v.seq: v for v in monitorlog.attribute(recs, "fyex")}
    crossed = next(v for v in truth.values() if v.crossed)
    got = list(me.read(e))
    assert {v.seq for v in got} == {
        s for s, v in truth.items()
        if abs(_ns(v.time) - _ns(crossed.time)) <= 60 * 10**9
    }  # fmt: skip
    for v in got:
        assert (v.who, v.container, v.crossed) == (
            truth[v.seq].who, truth[v.seq].container, truth[v.seq].crossed,
        )  # fmt: skip
        assert v.who != "container" or v.name == "fyex-devbox"
    # …with the store and its catalogue gone
    for p in (state / "logs").glob("monitor*"):
        p.unlink()
    assert list(me.read(me.kept()[0])) == got
    for p in [e.path, *e.path.iterdir(), me.root()]:
        assert not stat.S_IMODE(p.stat().st_mode) & 0o077, p


def test_nothing_is_kept_until_the_window_is_whole_or_its_boot_has_ended(state):
    s = Stream()
    recs = s.crossing()
    at = next(i for i, r in enumerate(recs) if _crossed(r))
    store(recs[: at + 8])
    assert me.kept() == []
    store([Stream("boot-2").snap()])  # the VM restarted: nothing more of that boot will come
    [e] = me.kept()
    assert e.boot == BOOT and e.hi == recs[at + 7]["seq"]


def _crossed(r: dict) -> bool:
    return "parent" in (r.get("e") or {}).get("process_exec", {})


def test_windows_do_not_overlap_and_a_crossing_in_one_is_only_listed(state):
    s = Stream()
    store(s.crossing(n=3))
    [e] = me.kept()
    assert len(e.crossings) == 3
    s.t -= 270  # 30 s past the first crossing's minute: its own window starts where that ended
    s.t += 60 + 30
    store([s.cross(), s.snap()])
    s.t += 300
    store([s.ev(), s.snap()])
    first, second = me.kept()
    assert first.id == e.id and second.lo > first.hi


def test_one_that_cannot_be_judged_is_kept_unconfirmed_and_goes_if_it_was_none(state, monkeypatch):
    for verdict, left in ((1, True), (0, False)):
        s = Stream(f"boot-{verdict}")
        with monkeypatch.context() as busy:
            busy.setattr(ms.Catalogue, "judge", lambda self: 1 / 0)
            store(s.crossing())
            [e] = [e for e in me.kept() if e.boot == s.boot]
            assert not e.confirmed
            with ms.Catalogue.open() as cat:
                cat.conn.execute(
                    "UPDATE crossing SET verdict = ?, container = ? WHERE boot = ?",
                    (verdict, BOX, s.boot),
                )
        ms.append([s.ev()])
        after = [e for e in me.kept() if e.boot == s.boot]
        assert bool(after) is left and all(e.confirmed for e in after)
    assert "judged not to be a crossing" in me.State.load().say[-1]["text"]


def _noise(n: int) -> str:
    return random.Random(n).randbytes(n).hex()


def _loud(s: Stream, n: int = 300) -> list[dict]:
    """A crossing whose window doesn't compress: about ``n`` × 30 bytes of it."""
    recs = s.crossing()
    for i, r in enumerate(recs):
        if r["k"] == "ev" and "parent" not in r["e"]["process_exec"]:
            r["e"]["process_exec"]["process"]["arguments"] = _noise(n + i)
    return recs


def test_a_boots_first_crossings_win_and_the_rest_are_counted(state, monkeypatch):
    s = Stream()
    store(_loud(s))
    size = me.kept()[0].size
    caps(monkeypatch, 4 * (size + size // 2))  # its boot's share: one and a half
    for _ in range(3):
        s.t += 600
        store(_loud(s))
    every = me.kept()
    assert every[0].seq == min(e.seq for e in every)
    assert sum(e.size for e in every) <= 4 * (size + size // 2) // 4 + 2000  # its context beside it
    assert me.State.load().past[BOOT] >= 1


def _fill(monkeypatch, boots: int = 2) -> tuple[list[me.Evidence], int]:
    """``boots`` boots with two windows each, and a cap they just fit."""
    for b in range(boots):
        s = Stream(f"old-{b}")
        s.t = b * 10_000
        store(_loud(s))
        s.t += 600
        store(_loud(s))
    every = me.kept()
    assert [e.boot for e in every] == [f"old-{b}" for b in range(boots) for _ in range(2)]
    limit = sum(e.size for e in every) + 100
    monkeypatch.setattr(me, "_BOOT", 1)
    monkeypatch.setattr(me, "_WINDOW", 1)
    caps(monkeypatch, limit)
    return every, limit


def _new(boot: str = "new") -> Stream:
    s = Stream(boot)
    s.t = 100_000
    return s


def test_full_a_new_window_waits_in_the_store_and_is_said_once(state, monkeypatch):
    old, _ = _fill(monkeypatch)
    s = _new()
    store(_loud(s))
    assert me.kept() == old and [p["boot"] for p in me.State.load().pending] == ["new"]
    logged, told = [], []
    for _ in range(2):
        me.tell(logged.append, lambda title, body: told.append(body))
        ms.append([s.ev()])
    assert len(told) == 1 and "full" in told[0] and len(logged) == 1
    assert "1 more wait" in me.note()
    assert me.remove(old[1].id) == 0  # the operator makes room: copied, nothing lost
    ms.append([s.ev()])
    assert [e.boot for e in me.kept()] == ["old-0", "old-1", "old-1", "new"]
    assert me.State.load().pending == [] and me.note() == ""


def test_the_oldest_makes_way_only_when_the_store_would_lose_the_new_one(state, monkeypatch):
    old, _ = _fill(monkeypatch)
    s = _new()
    store(_loud(s))
    monkeypatch.setattr(ms, "SEGMENT_BYTES", 10)
    ms.append([s.ev()])  # sealed
    assert me.kept() == old
    monkeypatch.setattr(ms, "budget", lambda: 1)  # retention now takes it
    ms.append([s.ev()])
    left = me.kept()
    # the oldest boot's LATER window went; every boot's first is still there
    assert [(e.boot, e.seq) for e in left[:-1]] == [(e.boot, e.seq) for e in old if e != old[1]]
    assert left[-1].boot == "new" and left[-1].confirmed
    assert "made way" in me.State.load().say[-1]["text"]
    assert len(list(me.read(left[-1]))) == left[-1].events


def test_the_first_ones_go_last_and_a_boot_never_evicts_its_own():
    def e(boot: str, seq: int) -> me.Evidence:
        return me.Evidence(id=f"{boot}-{seq}", boot=boot, seq=seq, time="", lo=seq, hi=seq)

    every = [e("a", 1), e("a", 5), e("a", 9), e("b", 2), e("b", 4), e("c", 3)]
    assert [v.id for v in me._victims(every, "c")] == ["a-9", "a-5", "b-4", "a-1", "b-2"]


def test_retention_takes_nothing_it_has_not_copied_out_even_of_an_open_window(state, monkeypatch):
    s = Stream()
    recs = s.crossing()
    at = next(i for i, r in enumerate(recs) if _crossed(r))
    monkeypatch.setattr(ms, "SEGMENT_BYTES", 10)
    store(recs[: at + 5])
    assert me.kept() == []
    monkeypatch.setattr(ms, "budget", lambda: 1)
    ms.append([recs[at + 5]])
    [e] = me.kept()
    assert e.lo <= recs[at]["seq"] <= e.hi and any("before they had all" in m for m in e.missing)


def test_a_window_past_its_share_keeps_what_is_nearest_the_crossing(state, monkeypatch):
    caps(monkeypatch, 16 * 4000)
    s = Stream()
    store(_loud(s, 400))
    [e] = me.kept()
    assert e.lo < e.seq < e.hi and e.size < 4000 + 3000
    assert len([m for m in e.missing if "past a window's share" in m]) == 2
    assert len(list(me.read(e))) == e.events


def test_a_crossing_past_where_a_window_was_cut_short_gets_its_own(state, monkeypatch):
    caps(monkeypatch, 16 * 4000)
    s = Stream()
    recs = _loud(s, 400)
    at = next(i for i, r in enumerate(recs) if _crossed(r))
    late = next(r for r in recs[at + 5 :] if r["k"] == "ev")  # in the minute, past the share
    late["e"]["process_exec"]["process"]["ns"]["mnt"]["inum"] = 1
    late["e"]["process_exec"]["parent"] = {"ns": {"mnt": {"inum": 200}}}
    store(recs)
    first, second = me.kept()
    assert first.hi < late["seq"] and second.lo <= late["seq"] <= second.hi


def test_a_cap_of_0_keeps_none(state, monkeypatch):
    (state / "monitor-store.toml").write_text("evidence_mb = 0\n")
    store(Stream().crossing())
    assert me.kept() == [] and not me.root().exists() and me.note() == ""


@pytest.mark.parametrize(
    ("text", "mb", "said"),
    [("", 256, ""), ("evidence_mb = 512", 512, ""), ("evidence_mb = 1", 64, "outside"),
     ("evidence_mb = 99999", 4096, "outside"), ('evidence_mb = "x"', 256, "whole number"),
     ("evidence_mb = true", 256, "whole number"), ("evidence_mb = 0", 0, ""), ("[", 256, "")],
)  # fmt: skip
def test_the_cap_is_the_operators(state, text, mb, said):
    (state / "monitor-store.toml").write_text(text)
    got, note = me.cap_setting()
    assert got == mb << 20 and (said in note if said else note == "")


def test_failing_to_keep_it_never_stops_the_store_and_is_retried_and_said(state, monkeypatch):
    s = Stream()
    recs = s.crossing()
    with monkeypatch.context() as broken:
        broken.setattr(me, "_copy", lambda *a: 1 / 0)
        store(recs)
        assert "can't be copied out of the store (ZeroDivisionError" in me.note()
    assert len([r for r, _ in ms.scan(None)]) == len(recs)
    ms.append([s.ev()])
    assert len(me.kept()) == 1 and me.note() == ""


GB = 1 << 30


@pytest.mark.parametrize(
    ("free", "budget", "held"),
    [
        (500 * GB, 8 * GB, 8 * GB),  # plenty: up to twice the budget
        (54 * GB, 8 * GB, 4 * GB),  # …but never so that less than 50 GB is left free
        (50 * GB, 8 * GB, 8 * GB // 10),  # a tenth of the budget otherwise
        (40 * GB, 8 * GB, 8 * GB // 10),
        (2 * GB, 8 * GB, 2 * GB // 10),  # or of what is free, when that is less
        (0, 8 * GB, 0),
        (None, 8 * GB, 8 * GB // 10),  # not known
    ],
)
def test_how_far_past_its_budget_the_store_may_be_held(state, monkeypatch, free, budget, held):
    def usage(path):
        if free is None:
            raise OSError("no such volume")
        return SimpleNamespace(free=free)

    monkeypatch.setattr(ms.shutil, "disk_usage", usage)
    assert ms.held_back(budget) == held


def _sealed_crossing(monkeypatch) -> tuple[Stream, int]:
    """A stored crossing, not yet copied out, in segments all but the last sealed."""
    s = Stream()
    recs = s.crossing()
    at = next(i for i, r in enumerate(recs) if _crossed(r))
    monkeypatch.setattr(ms, "SEGMENT_BYTES", 10)
    store(recs[: at + 5])
    monkeypatch.setattr(ms, "budget", lambda: 1)  # retention would now take all of them
    return s, recs[at]["seq"]


@pytest.mark.parametrize("why", ["catalogue", "copy"])
def test_retention_is_held_back_while_a_crossing_cannot_be_copied_out(state, monkeypatch, why):
    s, seq = _sealed_crossing(monkeypatch)
    allowance = [1 << 30]
    monkeypatch.setattr(ms, "held_back", lambda limit: allowance[0])
    with monkeypatch.context() as broken:
        if why == "catalogue":
            broken.setattr(ms.Catalogue, "open_or_none", classmethod(lambda cls: None))
        else:
            broken.setattr(me, "_copy", lambda *a: 1 / 0)
        before = len(ms.segments())
        for _ in range(3):
            ms.append([s.ev()])
        assert len(ms.segments()) >= before and any(r.get("seq") == seq for r, _ in ms.scan(None))
        assert "held back" in me.note()
    ms.append([s.ev()])  # it opens again: copied, and only then removed
    [e] = me.kept()
    assert e.lo <= seq <= e.hi and me.note() == ""
    assert not any(r.get("seq") == seq for r, _ in ms.scan(None))


def test_a_crossing_stored_while_the_catalogue_could_not_be_opened_is_copied_out(
    state, monkeypatch
):
    s = Stream()
    store([s.snap(), s.ev()])
    monkeypatch.setattr(ms, "SEGMENT_BYTES", 10)
    monkeypatch.setattr(ms, "held_back", lambda limit: 1 << 30)
    with monkeypatch.context() as locked:
        locked.setattr(ms.Catalogue, "open_or_none", classmethod(lambda cls: None))
        recs = s.crossing()
        seq = next(r["seq"] for r in recs if _crossed(r))
        store(recs)
    monkeypatch.setattr(ms, "budget", lambda: 1)
    ms.append([s.ev()])
    [e] = me.kept()
    assert e.confirmed and e.lo <= seq <= e.hi


def test_held_back_no_further_than_its_allowance(state, monkeypatch):
    s, seq = _sealed_crossing(monkeypatch)
    monkeypatch.setattr(ms.Catalogue, "open_or_none", classmethod(lambda cls: None))
    monkeypatch.setattr(ms, "held_back", lambda limit: 0)
    ms.append([s.ev()])
    assert not any(r.get("seq") == seq for r, _ in ms.scan(None))


def test_nothing_is_held_back_with_a_cap_of_0(state, monkeypatch):
    s, seq = _sealed_crossing(monkeypatch)
    caps(monkeypatch, 0)
    monkeypatch.setattr(ms, "held_back", lambda limit: 1 << 30)
    monkeypatch.setattr(ms.Catalogue, "open_or_none", classmethod(lambda cls: None))
    ms.append([s.ev()])
    assert not any(r.get("seq") == seq for r, _ in ms.scan(None))


def test_a_crash_at_any_point_keeps_it_once(state, monkeypatch):
    s = Stream()
    with monkeypatch.context() as crash:
        crash.setattr(me.State, "save", lambda self: None)  # copied; the note of it never written
        store(s.crossing())
    [e] = me.kept()
    (me.root() / ".new-half").mkdir()
    ms.append([s.ev()])
    assert me.kept() == [e] and not list(me.root().glob(".new-*"))
    for p in (state / "logs").glob("monitor-index.sqlite*"):
        p.unlink()  # the catalogue lost, and the state with it
    me._state_file().unlink(missing_ok=True)
    with ms.Catalogue.open() as cat:
        cat.catch_up()
    ms.append([s.ev()])
    assert me.kept() == [e]


def test_one_removed_by_the_operator_does_not_come_back(state):
    s = Stream()
    store(s.crossing())
    assert me.remove(me.kept()[0].id) == 0
    ms.append([s.ev()])
    assert me.kept() == []


def test_a_boots_name_never_names_a_path(state):
    for boot in ("../../up", ".", "a/b", "x" * 200):
        assert "/" not in me._id(boot, 3) and not me._id(boot, 3).startswith(".")
    store(Stream("../../up").crossing())
    [e] = me.kept()
    assert e.path.parent == me.root() and e.boot == "../../up"


def test_the_command_lists_prints_and_removes_it(state, monkeypatch):
    run = CliRunner().invoke
    assert "no evidence kept" in run(cli.app, ["monitor", "evidence"]).output
    recs = Stream().crossing()
    next(r for r in recs if _crossed(r))["e"]["process_exec"]["process"]["arguments"] = "\x1b[2J hi"
    store(recs)
    [e] = me.kept()
    out = run(cli.app, ["monitor", "evidence"]).output
    assert e.id in out and BOX[:12] in out and "unconfirmed" not in out
    got = run(cli.app, ["monitor", "evidence", e.id[:8]])
    assert got.exit_code == 0 and "⚠ crossed" in got.output and "\x1b" not in got.output
    lines = run(cli.app, ["monitor", "evidence", e.id, "--json"]).output.splitlines()
    assert len(lines) == e.events and json.loads(lines[0])["boot"] == BOOT
    for bad in ("../..", "nope", "/etc"):
        assert run(cli.app, ["monitor", "evidence", bad, "--remove", "--yes"]).exit_code == 1
    assert run(cli.app, ["monitor", "evidence", "--remove"]).exit_code == 1
    assert run(cli.app, ["monitor", "evidence", e.id, "--remove"], input="n\n").exit_code == 1
    assert me.kept() == [e]
    assert run(cli.app, ["monitor", "evidence", e.id, "--remove", "--yes"]).exit_code == 0
    assert me.kept() == []
    monkeypatch.setattr(ms.config, "in_box", lambda: True)
    assert run(cli.app, ["monitor", "evidence"]).exit_code == 1


def test_fy_monitor_and_doctor_say_what_is_kept_and_what_is_wrong(state, monkeypatch, capsys):
    from foldyard import devmode

    store(Stream().crossing())
    monitorlog.Cursor(pulled=1.0).save()
    assert "evidence of 1 crossings kept" in monitorlog.store_summary()
    monkeypatch.setattr(me, "note", lambda: "the evidence is full")
    assert monitorlog.show(limit=1) == 0
    assert "⚠ the evidence is full" in capsys.readouterr().out
    import time

    monitorlog.Cursor(pulled=time.time()).save()
    [row] = list(devmode._monitor_events_check())
    assert row[0] == "warn" and "the evidence is full" in row[2]
