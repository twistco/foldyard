"""Property-based tests (hypothesis) for the guest monitor's host side: what a hook delivers,
against the join over EVERY record the pull ever stored.

The bug this pins (2026-10-21, docs/monitor-pipeline.md §6): a hook joined events to containers
over a byte window around its position, so whenever two relay snapshots were further apart in the
store than the window — a storm of a few hundred events a second — the events between them had
neither snapshot in view, were released on age as ``unattributed``, filtered out by
``who = ["container"]``, and passed by for good. Nothing said so.

The property: run the real pull-side store (``monitorlog._append``) and the real hook rounds
(``monitorhooks.round_once``) over a GENERATED stream — snapshots arbitrarily far apart against a
window a few records wide, engine claims long before the events they name, transport and relay
gaps, VM restarts, rotation with a tiny or a generous budget, several hooks with different
filters, rounds interleaved with pulls — and hold every hook's deliveries against
``attribute(all records)`` filtered by that hook. Allowed differences must be SAID: an event the
hook never got, or got attributed differently, lies inside a ``gaps`` entry it was sent.

Validated red against the code before the fix: a new hook slid along with the tail while it had
nothing settled to send (shrinks to four events in one pull), and — anchored by hand — the
windowed join (shrinks to a snapshot and three events: attributed ``unattributed`` where the full
join says ``vm``, nothing in ``gaps``). After the fix, red against each of: joining without the
catalogue's snapshots around the read, dropping the ``rotated`` gap, re-anchoring a new hook every
round, no ``unsettled`` gap for an event released on age, a read that may end before any event
(the cursor stalls), and the catalogue's snapshots put before the read (events then come out of
store order) — the last three pinned by the @examples below, which random search finds rarely.

The 0.6.1 upgrade (cabash, 2026-10-22): the generator's ``upgrade`` step turns the store into what
0.6.0 left (a live file with no first line, which no catalogue has seen; hooks at inode positions)
and races the next round against the pull's first append, which seals that file under a new id;
``behind`` puts a hook where the false "lost" this caused put it — at the oldest record, behind its
own cursor. Red against 0.6.1 on both (the two @examples after the first three: "rotated, with room
to spare", then an event never delivered — the hook stalled for good), and after the fix against
not indexing an older foldyard's live file before sealing it (its snapshots go with retention
unseen, and events after them are attributed wrong) and against leaving a hook that is behind its
cursor where it is.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import event, example, given, settings
from hypothesis import strategies as st

from foldyard import monitorhooks as mh
from foldyard import monitorlog, monitorstore
from pbt import scaled_examples

A, B, C = "a" * 64, "b" * 64, "c" * 64
META = {
    A: {"name": "fyex-api-1", "project": "fyex", "service": "api"},
    B: {"name": "fyex-feat-devbox", "project": "", "service": ""},
    C: None,  # gone before the host asked
}
INODES = (1, 200, 300, 400)  # 1 is the VM's own namespace
PODMAN_NS = 7  # podman's rootless namespace, where conmon runs
KINDS = ("process_exec", "process_exit")

# ── the generated world, as plain data ─────────────────────────────────────────────────

mappings = st.dictionaries(
    st.sampled_from(INODES[1:]),
    st.sampled_from([[A], [B], [C], [A, B]]),
    max_size=3,
)
steps = st.lists(
    st.one_of(
        st.tuples(st.just("ev"), st.sampled_from(INODES), st.sampled_from(KINDS)),
        st.tuples(st.just("ev"), st.sampled_from(INODES), st.sampled_from(KINDS)),
        st.tuples(st.just("ev"), st.sampled_from((*INODES, PODMAN_NS)), st.sampled_from(KINDS)),
        st.tuples(st.just("burst"), st.integers(5, 40), st.sampled_from(INODES)),
        # a container's init entering (exec) or leaving (exit) its namespace: a mark the join reads
        st.tuples(st.just("init"), st.sampled_from(INODES[1:]), st.sampled_from(KINDS)),
        # podman entering a namespace from the VM's (its own: 7, where conmon runs; or a helper's),
        # and an image build step's init, started by podman itself there
        st.tuples(st.just("entry"), st.sampled_from((PODMAN_NS, *INODES[1:]))),
        st.tuples(st.just("build"), st.sampled_from(INODES[1:]), st.sampled_from(KINDS)),
        st.tuples(st.just("snap"), mappings),
        st.tuples(st.just("engine"), st.sampled_from([A, B, C])),
        st.tuples(st.just("lost"), st.integers(1, 3)),
        st.just(("relaygap",)),
        st.just(("boot",)),
        st.just(("pull",)),
        st.just(("round",)),
        st.just(("upgrade",)),
        st.just(("behind",)),
        st.tuples(st.just("wait"), st.sampled_from([1.0, 30.0, 200.0])),
    ),
    min_size=8,
    max_size=60,
)
# a process in the VM's namespace (1: by the snapshots, or Tetragon's is_host too) or podman's (7:
# podman's only once something entered it from the VM's), its parent in a container's
crossing = st.tuples(
    st.just("cross"), st.sampled_from((1, PODMAN_NS)), st.sampled_from(INODES[1:]), st.booleans()
)
hooks = st.lists(
    st.tuples(
        st.lists(st.sampled_from(mh.WHO), unique=True, max_size=2),
        st.lists(st.sampled_from(["exec", "exit"]), unique=True, max_size=1),
        st.lists(st.sampled_from(["main", "feat"]), unique=True, max_size=1),
    ),
    min_size=1,
    max_size=3,
)


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class World:
    """The pull, simulated at the record level: what ``ingest`` + ``pull_once`` append."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0
        self.boot_n = 0
        self.boot = "boot-0"
        self.seq = 0
        self.pending: list[dict] = []
        self.all: list[dict] = []  # every record ever appended, in store order
        self.described: set[str] = set()
        self.states: dict[str, dict] = {}
        self.anchorless: set[str] = set()  # hooks an upgrade left with neither cursor nor place

    def _rec(self, body: dict) -> None:
        self.pending.append({**body, "boot": self.boot, "seq": self.seq})
        self.seq += 1

    def step(self, s: tuple) -> None:
        what = s[0]
        if what in ("ev", "init", "build", "entry", "cross"):
            proc = {"binary": f"/bin/b{s[1]}", "pid": 7, "uid": 1, "ns": {"mnt": {"inum": s[1]}},
                    "start_time": _iso(self.now)}  # fmt: skip
            body: dict = {"process": proc}
            if what == "cross":
                if s[3] and s[1] == 1:
                    proc["ns"]["mnt"]["is_host"] = True
                body["parent"] = {"binary": "/bin/sh", "ns": {"mnt": {"inum": s[2]}}}
            if what in ("init", "build"):
                proc["in_init_tree"] = True
                by = "/usr/bin/conmon" if what == "init" else "/proc/self/exe"
                body["parent"] = {"binary": by, "ns": {"mnt": {"inum": PODMAN_NS}}}
            if what == "entry":
                proc["ns"]["user"] = {"inum": 4026532575}
                body["parent"] = {"ns": {"mnt": {"inum": 1, "is_host": True}}}
            kind = "process_exec" if what in ("entry", "cross") else s[2]
            self._rec({"k": "ev", "e": {kind: body, "time": _iso(self.now)}})
        elif what == "burst":  # a storm: many events, no snapshot among them
            for i in range(s[1]):
                self.step(("ev", s[2] if i % 3 else INODES[i % len(INODES)], KINDS[i % 2]))
        elif what == "snap":
            self._rec(
                {"k": "scopes", "t": self.now, "vm": 1, "map": {str(k): v for k, v in s[1].items()}}
            )
        elif what == "engine":  # a claim far from the events it names
            self.pending.append({"k": "engine", "containers": {s[1]: META[s[1]]}})
            self.described.add(s[1])
        elif what == "lost":  # sequence numbers that never arrived, as ingest records them
            self.pending.append(
                {"k": "gap", "boot": self.boot, "from": self.seq, "to": self.seq + s[1] - 1}
            )
            self.seq += s[1]
        elif what == "relaygap":
            self._rec({"k": "gap", "t": self.now, "why": "tetragon's export rotated unseen"})
        elif what == "boot":
            self.boot_n += 1
            self.boot, self.seq = f"boot-{self.boot_n}", 0
        elif what == "pull":
            self.pull()
        elif what == "round":
            mh.round_once(now=self.now)
        elif what == "wait":
            self.now += s[1]
        elif what == "upgrade":
            self.upgrade()
        elif what == "behind":  # what 0.6.1's false "rotated" left: the oldest record's position
            # (in the cursor's own boot: there, every read was events it already had — a stall;
            # from an older boot's, 0.6.1 re-sent them instead)
            # — only while the store still held the cursor's event: that is what made the "lost"
            # false. Once retention had removed it, 0.6.1's "lost" was true and said `rotated`.
            first = next((r for r, _ in monitorstore.scan(None) if r.get("k") == "ev"), None)
            oldest = [monitorstore.segments()[0].id, 0] if first else []
            for h in self._states():
                last = h.get("last")
                if first and last and last[0] == first["boot"] and int(first["seq"]) <= last[1]:
                    h["pos"] = oldest
            self._save()

    def states_now(self) -> dict[str, dict]:
        path = mh._state_file()
        return json.loads(path.read_text()) if path.exists() else {}

    def _states(self) -> list[dict]:
        path = mh._state_file()
        self.states = json.loads(path.read_text()) if path.exists() else {}
        return list(self.states.values())

    def _save(self) -> None:
        mh._state_file().write_text(json.dumps(self.states))

    def upgrade(self) -> None:
        """0.6.0 → 0.6.1 at this point: the store an older foldyard leaves (a live file with no
        first line naming it, no catalogue), hooks holding its positions (an inode and an offset),
        and the new supervisor's first hook round racing its first pull — the round lists the
        store, the pull seals the older live file under a new id, and only then does the round's
        catalogue catch up, with the listing from before (cabash, 2026-10-22)."""
        live = monitorstore.live_path()
        if not live.exists():
            return
        lines = live.read_bytes().split(b"\n", 1)
        if b'"k":"segment"' in lines[0]:
            live.write_bytes(lines[1] if len(lines) > 1 else b"")
        with monitorstore.Catalogue.open() as cat:  # which no catalogue has seen
            sid = json.loads(lines[0]).get("id")
            for table in ("seg WHERE id", "span WHERE seg", "snap WHERE seg"):
                cat.conn.execute(f"DELETE FROM {table} = ?", (sid,))
        self._states()
        for name, h in self.states.items():
            if h.get("pos"):
                h["pos"] = [live.stat().st_ino, 99]
                if not h.get("last"):  # nothing to find it again by: a gap is all it can say
                    self.anchorless.add(name)
        self._save()
        real = monitorlog.catalogue

        def catalogue(segs=None):
            before = monitorstore.segments()
            self.pending.append({"k": "engine", "containers": {}})  # the pull always writes
            self.pull()
            return real(before)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(monitorlog, "catalogue", catalogue)
            mh.round_once(now=self.now)

    def pull(self) -> None:
        # as pull_once does: every container a snapshot names is described once, in that pull
        named = {c for r in self.pending if r["k"] == "scopes" for ids in r["map"].values()
                 for c in ids}  # fmt: skip
        new = sorted(named - self.described)
        if new:
            self.pending.append({"k": "engine", "containers": {c: META[c] for c in new}})
            self.described |= set(new)
        if self.pending:
            recs = [{**r, "recv": self.now} for r in self.pending]
            monitorlog._append(recs)
            self.all.extend(recs)
            self.pending = []


def _order(world: World):
    boots = list(dict.fromkeys(r["boot"] for r in world.all if "boot" in r))

    def rank(key) -> tuple[int, int]:
        boot, seq = key
        return (boots.index(boot) if boot in boots else len(boots), int(seq))

    return rank


def _covered(key, gaps: list[dict], rank, whys: tuple[str, ...]) -> bool:
    lo_inf, hi_inf = (-1, -1), (1 << 30, 1 << 30)
    for g in gaps:
        if g.get("why") not in whys:
            continue
        lo = rank(g["after"]) if g.get("after") else lo_inf
        hi = rank(g["before"]) if g.get("before") else hi_inf
        if lo < rank(key) < hi:
            return True
    return False


EVERYTHING = [([], [], [])]


@settings(max_examples=scaled_examples(200), deadline=None)
@given(scenario=steps, filters=hooks, budget=st.sampled_from(["tiny", "ample"]))
# released on age, then a snapshot names its container: the hook must say it went without one
@example(
    scenario=[("snap", {}), ("ev", 200, "process_exec"), ("pull",), ("wait", 200.0), ("round",),
              ("snap", {200: [A]}), ("pull",), ("round",)],
    filters=EVERYTHING, budget="ample",
)  # fmt: skip
# claims and a snapshot wider than a read: the read must go on to an event
@example(
    scenario=[("ev", 1, "process_exec"), ("engine", A), ("engine", B),
              ("snap", {200: [A], 300: [A], 400: [A, B]}), *[("ev", 1, "process_exec")] * 4],
    filters=EVERYTHING, budget="tiny",
)  # fmt: skip
# a VM restart inside the settle window: the old boot's tail must come before the new boot's
@example(
    scenario=[("ev", 1, "process_exec"), ("boot",), ("ev", 1, "process_exec"), ("snap", {}),
              ("relaygap",), ("pull",), ("snap", {}), ("round",)],
    filters=EVERYTHING, budget="tiny",
)  # fmt: skip
# 0.6.1's upgrade: the segment sealed under the round's catalogue held the cursor — not "rotated"
@example(
    scenario=[("snap", {}), ("ev", 1, "process_exec"), ("snap", {}), ("pull",), ("round",),
              ("ev", 1, "process_exec"), ("snap", {}), ("pull",), ("upgrade",)],
    filters=EVERYTHING, budget="ample",
)  # fmt: skip
# …and where that left a hook: behind its own cursor by more than a read — it must walk on
@example(
    scenario=[("snap", {}), ("burst", 8, 1), ("snap", {}), ("pull",), ("round",),
              ("ev", 1, "process_exec"), ("snap", {}), ("pull",), ("behind",)],
    filters=EVERYTHING, budget="ample",
)  # fmt: skip
# two snapshots naming different containers, nothing to place the event: a container-only hook
@example(
    scenario=[("snap", {200: [A]}), ("ev", 200, "process_exec"), ("snap", {200: [B]}), ("pull",)],
    filters=[(["container"], [], [])], budget="ample",
)  # fmt: skip
# a reused number decided by a container's init mark in an EARLIER read than the event: the hook
# must join with the catalogue's marks, as it does with its snapshots
@example(
    scenario=[("snap", {200: [A]}), ("wait", 1.0), ("init", 200, "process_exec"),
              *[("ev", 1, "process_exec")] * 4, ("ev", 200, "process_exec"), ("snap", {200: [B]}),
              ("pull",), ("round",)],
    filters=EVERYTHING, budget="ample",
)  # fmt: skip
# retention right after a seal, the live segment holding no record of the running boot yet (an
# engine-only append): the boot's facts must stay — podman's namespace was forgotten (#138's review)
@example(
    scenario=[("entry", 7), ("entry", 7), ("entry", 200), ("pull",), ("upgrade",), ("entry", 7),
              ("ev", 200, "process_exec"), ("snap", {})],
    filters=EVERYTHING, budget="tiny",
)  # fmt: skip
# retention kept podman's later entry into its namespace and dropped the one before a kept init's
# exit came from it: that init then read as one from nowhere, and its container's life went
# unbounded (#138, the randomised runs)
@example(
    scenario=[("ev", 1, "process_exec"), ("ev", 1, "process_exec"), ("entry", 7), ("pull",),
              ("entry", 200), ("init", 300, "process_exit"), ("entry", 7), ("pull",),
              ("ev", 300, "process_exec"), ("snap", {300: [A]})],
    filters=EVERYTHING, budget="tiny",
)  # fmt: skip
# 0.6.1's false "lost" only where the cursor's event was still stored: here retention had removed
# the stretch after it (podman's entry among it), and the hook owes `rotated`, not a silent skip
@example(
    scenario=[*[("ev", 1, "process_exec")] * 3, ("snap", {200: [A], 300: [A]}), ("entry", 7),
              ("upgrade",), ("ev", 1, "process_exec"), ("pull",), ("behind",)],
    filters=EVERYTHING, budget="tiny",
)  # fmt: skip
def test_a_hook_delivers_the_full_store_join_or_says_what_it_could_not(
    monkeypatch, scenario, filters, budget
):
    with tempfile.TemporaryDirectory() as tmp:
        _check(_drive(monkeypatch, Path(tmp), scenario, filters, budget), budget)


def _drive(monkeypatch, state: Path, scenario, filters, budget) -> dict:
    """The world's stream through the real store and the real hook rounds, drained: every hook's
    batches, beside the full-store join (``truth``)."""
    monkeypatch.setattr(mh.config, "state_dir", lambda: state)
    monkeypatch.setattr(mh.config, "project", lambda: "fyex")
    monkeypatch.setattr(mh.config, "project_prefix", lambda: "fyex")
    # windows a few records wide, against snapshots any distance apart
    monkeypatch.setattr(mh, "READ_BYTES", 700)
    # segments of a few records, and a budget that keeps only the newest — or everything
    monkeypatch.setattr(monitorstore, "SEGMENT_BYTES", 1500)
    keep = 1 if budget == "tiny" else 1 << 30
    monkeypatch.setattr(monitorstore, "budget", lambda: keep)

    got: dict[str, list[dict]] = {}

    def fake_run(argv, input, capture_output, timeout, env):
        got.setdefault(env["FY_HOOK"], []).append(json.loads(input))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(mh.subprocess, "run", fake_run)
    text = ""
    made = []
    for i, (who, kinds, worktrees, *keep_who) in enumerate(filters):
        hook = mh.Hook(name=f"h{i}", command=("/bin/h",), who=tuple(who), kinds=tuple(kinds),
                       worktrees=tuple(worktrees),
                       keep_who_after_crossing=bool(keep_who and keep_who[0]))  # fmt: skip
        made.append(hook)
        text += mh.render(hook) + "\n"
    (state / "monitor-hooks.toml").write_text(text)

    world = World()
    # the relay's first record, then a round: each hook takes its place in the stream
    world.pending.append({"k": "start", "boot": world.boot, "seq": 0})
    world.seq = 1
    world.pull()
    mh.round_once(now=world.now)
    for s in scenario:
        world.step(s)
    world.pull()
    world.now += 10_000  # long past the settle window: everything is deliverable
    before = None
    for _ in range(2000):  # drain: until a round changes nothing
        mh.round_once(now=world.now)
        snapshot = (state / "monitor-hooks-state.json").read_text()
        if snapshot == before:
            break
        before = snapshot

    truth = {(e.boot, e.seq): asdict(e) for e in monitorlog.attribute(world.all, "fyex")}
    # each boot's first crossing, by the full join — what the hooks' switch must agree with
    first: dict[str, int] = {}
    for (boot, seq), e in truth.items():
        if e["crossed"]:
            first[boot] = min(first.get(boot, seq), seq)
    return {"world": world, "hooks": made, "got": got, "truth": truth, "first": first,
            "rank": _order(world)}  # fmt: skip


def _after_crossing(run: dict, key: tuple[str, int]) -> bool:
    return key[0] in run["first"] and key[1] >= run["first"][key[0]]


def _check(run: dict, budget: str) -> None:
    """The hook property: each hook's deliveries are the full-store join filtered by that hook, or
    a gap says why not; nothing it passed over goes uncounted."""
    world, truth, rank = run["world"], run["truth"], run["rank"]
    event(f"records>={min(len(world.all) // 50 * 50, 200)}")
    for hook in run["hooks"]:
        batches = run["got"].get(hook.name, [])
        gaps = [g for b in batches for g in b.get("gaps", [])]
        delivered: dict[tuple[str, int], dict] = {}
        for b in batches:
            for e in b["events"]:
                key = (e["boot"], e["seq"])
                assert delivered.setdefault(key, e) == e, "a re-sent event changed"
        want = {k: e for k, e in truth.items()
                if hook.matches(monitorlog.Event(**e), _after_crossing(run, k))}  # fmt: skip
        for key, e in want.items():
            if key not in delivered:
                assert _covered(key, gaps, rank, ("rotated", "unsettled")), (
                    f"{hook.name} never got {key} and no gap says so"
                )
            elif delivered[key] != e:
                assert _covered(key, gaps, rank, ("unsettled",)), (
                    f"{hook.name} got {key} attributed {delivered[key]['who']}, "
                    f"the full join says {e['who']}"
                )
        # an event the join couldn't place between two containers, that this hook would want
        # were it placed: passed over, it must be said — never read as "nothing happened"
        for key, e in truth.items():
            could = (not hook.kinds or e["kind"] in hook.kinds) and (
                not hook.who or "container" in hook.who
            )
            if e["who"] == "ambiguous" and key not in want and could:
                assert _covered(key, gaps, rank, ("ambiguous", "rotated", "unsettled")), (
                    f"{hook.name} passed over ambiguous {key} and no gap says so"
                )
        for key in delivered:
            if key not in want:
                assert _covered(key, gaps, rank, ("unsettled",)), (
                    f"{hook.name} got {key}, which its filter doesn't match in the full join"
                )
        # never silently dropped: an event its filters pass over (the VM's and podman's own,
        # by default) is counted in a batch's `passed`, or still owed to the next one
        counted: dict[str, int] = {}
        for b in batches:
            for w, n in b["passed"].items():
                counted[w] = counted.get(w, 0) + n
        owed = world.states_now().get(hook.name, {}).get("passed", {})
        for w in mh.WHO:
            over = [k for k, e in truth.items() if e["who"] == w and k not in delivered
                    and not _covered(k, gaps, rank, ("rotated", "unsettled"))]  # fmt: skip
            assert len(over) <= counted.get(w, 0) + owed.get(w, 0), (
                f"{hook.name} passed over {len(over)} {w} events, counted "
                f"{counted.get(w, 0)} + {owed.get(w, 0)} owed"
            )
        event(f"delivered>0={bool(delivered)}")
        event(f"whys={sorted({g['why'] for g in gaps})}")
        if budget == "ample":
            rotated = [g for g in gaps if g["why"] == "rotated"]
            if hook.name in world.anchorless:
                rotated = [g for g in rotated if g["after"] is not None]
            assert not rotated, "rotated, with room to spare"


# ── a crossing unfilters the rest of its boot ───────────────────────────────────────────
#
# The follow-up to the default filter (docs/monitor-pipeline.md §6): a process stepping out of a
# container is `crossed` and reaches every hook, but what it does next is in the VM's or podman's
# namespace, which a hook without `who` skips. So from a boot's first crossing on, `who` filters
# nothing of that boot — for every hook but one that keeps it (`keep_who_after_crossing`). The
# crossing is a fact of the store (the catalogue judges it once a snapshot after it is stored), so
# every hook agrees, at any cursor, through rotation and the 0.6 upgrade the world above plays.
# The claim, against the full-store join: after a boot's first crossing, nothing of that boot is
# passed over by `who` (an event a hook didn't get is one its kinds/worktrees don't want, or a gap
# says why), and each batch carrying such an event says so (`unfiltered`); before it, a hook
# gets exactly what its filter alone would give it; and the whole hook property above still holds.
# Validated red against: the switch never turned on (`deliver` without the crossings), turned on
# for the whole boot (the seq ignored), `keep_who_after_crossing` ignored, and the batch's
# `unfiltered` left empty.

with_a_crossing = st.tuples(steps, st.integers(0, 60), crossing, st.integers(0, 60), crossing).map(
    lambda t: [*t[0][: t[1]], t[2], *t[0][t[1] : t[3]], t[4], *t[0][t[3] :]]
)
crossing_hooks = st.lists(
    st.tuples(
        st.lists(st.sampled_from(mh.WHO), unique=True, max_size=2),
        st.lists(st.sampled_from(["exec", "exit"]), unique=True, max_size=1),
        st.just([]),
        st.booleans(),
    ),
    min_size=1,
    max_size=3,
)


@settings(max_examples=150, deadline=None)
@given(scenario=with_a_crossing, filters=crossing_hooks, budget=st.sampled_from(["tiny", "ample"]))
# the VM's events after a crossing into the VM's namespace: a default hook must get them
@example(
    scenario=[("snap", {200: [A]}), ("ev", 1, "process_exec"), ("cross", 1, 200, False),
              ("ev", 1, "process_exec"), ("snap", {200: [A]}), ("pull",), ("round",)],
    filters=[([], [], [], False)], budget="ample",
)  # fmt: skip
# …and nothing of the boot before the crossing; a hook that keeps its `who` keeps it
@example(
    scenario=[("snap", {200: [A]}), ("ev", 1, "process_exec"), ("ev", 1, "process_exec"),
              ("snap", {200: [A]}), ("pull",), ("round",), ("cross", 1, 200, True),
              ("ev", 1, "process_exec"), ("snap", {200: [A]}), ("pull",)],
    filters=[([], [], [], False), (["container"], [], [], True)], budget="ample",
)  # fmt: skip
def test_after_a_crossing_nothing_of_its_boot_is_skipped_and_before_it_nothing_changes(
    monkeypatch, scenario, filters, budget
):
    with tempfile.TemporaryDirectory() as tmp:
        run = _drive(monkeypatch, Path(tmp), scenario, filters, budget)
        _check(run, budget)  # inside: it reads what each hook still owes from the state dir
    truth, rank = run["truth"], run["rank"]
    event(f"crossed boots={len(run['first'])}")
    for hook in run["hooks"]:
        batches = run["got"].get(hook.name, [])
        gaps = [g for b in batches for g in b.get("gaps", [])]
        sent = {(e["boot"], e["seq"]): b for b in batches for e in b["events"]}
        for key, e in truth.items():
            ev = monitorlog.Event(**e)
            after = _after_crossing(run, key)
            excused = _covered(key, gaps, rank, ("rotated", "unsettled"))
            if after and not hook.keep_who_after_crossing:
                wanted_anyway = hook.matches(ev, True)  # what kinds/worktrees still leave out
                if wanted_anyway and not excused:
                    assert key in sent, f"{hook.name} skipped {key} ({e['who']}) after a crossing"
                    b = sent[key]
                    if not hook.matches(ev, False):  # got it only because of the crossing: said
                        assert any(u["boot"] == key[0] and u["since"][1] <= key[1]
                                   for u in b["unfiltered"]), f"{key} sent unfiltered, unsaid"  # fmt: skip
            elif not _covered(key, gaps, rank, ("unsettled",)):
                # before it (or a hook that keeps its `who`): exactly what the filter alone gives
                assert (key in sent) <= hook.matches(ev, False), (
                    f"{hook.name} got {key} ({e['who']}) before its boot's crossing"
                )


# ── the join over a reused namespace number ─────────────────────────────────────────────
#
# The bug (cabash, 2026-10-23, docs/monitor-pipeline.md §3): the kernel hands a freed mount-namespace
# number straight to the next namespace made (the lowest free one), so a container that starts
# right after another died often gets its number. The relay's last snapshot still names the dead
# container for it, the next one names the new container, and every event in between — the new
# container's opening commands, the old one's last — came out `ambiguous`, which a
# `who = ["container"]` hook drops. What tells the two apart is in the stream itself: a container's
# init enters the namespace from outside it (its parent is conmon) as part of the init tree, and
# leaves the same way. The join reads those marks — and only to choose between the two containers
# the kernel snapshots named.
#
# The world below is the kernel's, as plain data: containers started and stopped on two numbers
# handed out lowest-free, podman's own short-lived namespaces taking a free number, snapshots of
# what is running at any moment (also mid-setup: named before its init's exec reaches the stream),
# containers whose init Tetragon never shows as one (every life of it, both ends: say one sharing
# another's pid namespace; its exec is there, outside the init tree), and a dead container's event
# arriving late (never past a snapshot). What it does NOT model is a mark lost at random —
# Tetragon's ring buffer overflowing: a container's start seen and its exit lost, with an unmarked
# one taking the number next, would mislead the join. On cabash (2026-10-23) every init exec had its
# exit; nothing showed a loss.
# Validated red: before the marks were read (completeness, both directions), and
# after it against each of: no process-start guard (a late event of the dead container named the
# new one), no "no init exit after the last start" check, and no "no init start before the first
# exit" check (each a wrong container). The guards against what the model doesn't do — a mark lost
# at random, the two clocks apart — and what a mark is (tests/test_monitorstore.py) are pinned by
# unit tests instead.

NS_CIDS = ("d" * 64, "e" * 64, "f" * 64)
NS_NUMBERS = (200, 300)
CONMON_NS = 7  # where conmon runs: outside every container's mount namespace

_ns_step = st.one_of(
    # (container, Tetragon marks its init — fixed at its first start, a snapshot caught it
    # mid-setup)
    st.tuples(st.just("start"), st.integers(0, 2), st.booleans(), st.booleans()),
    st.tuples(st.just("run"), st.integers(0, 2), st.booleans()),  # podman exec, or a child
    st.tuples(st.just("init"), st.integers(0, 2)),  # its init, still running, does something
    st.tuples(st.just("stop"), st.integers(0, 2)),
    st.tuples(st.just("late"), st.integers(0, 2)),
    st.just(("transient",)),  # podman's own short-lived mount namespace (netavark)
    st.just(("snap",)),
    st.just(("snap",)),
)
ns_steps = st.lists(_ns_step, min_size=4, max_size=50)


def _stamp(ns: int) -> str:
    """A Tetragon timestamp: RFC 3339, nanoseconds with trailing zeros trimmed (as Go writes)."""
    secs, frac = divmod(ns, 10**9)
    head = datetime.fromtimestamp(secs, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return head + (f".{frac:09d}".rstrip("0") if frac else "") + "Z"


class NsWorld:
    """Records as the relay writes them, each event with the container that really caused it."""

    def __init__(self) -> None:
        self.t = 1_800_000_000 * 10**9
        self.recs: list[dict] = []
        self.truth: dict[int, str | None] = {}  # seq -> container (None: not a container's)
        self.recs.append(_entry(0, "b", _stamp(self.t), CONMON_NS))  # podman's namespace, at boot
        self.truth[0] = None
        self.running: dict[str, tuple[int, int, bool]] = {}  # cid -> (number, init start, seen)
        self.dead: dict[str, tuple[int, int, int]] = {}  # cid -> (number, init start, snaps then)
        self.snaps = 0
        self.life: dict[int, tuple[str, int]] = {}  # seq -> (cid, life no.)
        self.lives = 0
        self.cur: dict[str, int] = {}  # cid -> its current life no.
        self.exits: dict[int, int] = {}  # life no. -> seq of its init's exit (when seen)
        self.starts: dict[int, int] = {}  # life no. -> seq of its init's start (when seen)
        self.late: set[int] = set()  # events the stream delivered after their container died
        self.marked: dict[str, bool] = {}  # cid -> Tetragon shows its init as one

    def _tick(self) -> int:
        self.t += 7_340_000  # never a whole second: the stamps keep a fraction
        return self.t

    def _free(self) -> int | None:
        used = {n for n, _, _ in self.running.values()}
        return next((n for n in NS_NUMBERS if n not in used), None)

    def _ev(self, kind, number, pstart, init, parent_ns, cid, binary="/bin/sh") -> int:
        seq = len(self.recs)
        # a container's processes are in its own pid namespace (each life a new one); podman's
        # tools in the VM's
        life = self.cur.get(cid, 0) if cid is not None else 0
        pid = {"inum": 5000 + life} if cid is not None else {"inum": 4026531836, "is_host": True}
        proc = {"binary": binary, "pid": 1000 + seq, "uid": 0, "in_init_tree": init,
                "start_time": _stamp(pstart), "ns": {"mnt": {"inum": number}, "pid": pid}}  # fmt: skip
        body = {"process": proc, "parent": {"binary": "/usr/bin/conmon",
                                            "ns": {"mnt": {"inum": parent_ns}}}}  # fmt: skip
        self.recs.append({"k": "ev", "boot": "b", "seq": seq,
                          "e": {kind: body, "time": _stamp(self._tick())}})  # fmt: skip
        self.truth[seq] = cid
        if cid is not None and cid in self.cur:
            self.life[seq] = (cid, self.cur[cid])
        return seq

    def _snap(self) -> None:
        seq = len(self.recs)
        self.recs.append({"k": "scopes", "boot": "b", "seq": seq, "vm": 1, "t": self._tick() / 1e9,
                          "map": {str(n): [c] for c, (n, _, _) in self.running.items()}})  # fmt: skip
        self.snaps += 1

    def step(self, s: tuple[Any, ...]) -> None:
        what, *args = s
        cid = NS_CIDS[args[0]] if args else ""
        if what == "start" and cid not in self.running and (number := self._free()) is not None:
            seen, mid_setup = self.marked.setdefault(cid, args[1]), args[2]
            start = self._tick()
            self.running[cid] = (number, start, seen)
            self.dead.pop(cid, None)
            self.lives += 1
            self.cur[cid] = self.lives
            if mid_setup:
                self._snap()  # its scope already holds the runtime's process in the namespace
            # its init's exec, from conmon in podman's namespace — in the init tree when Tetragon
            # shows it as one
            seq = self._ev("process_exec", number, start, seen, CONMON_NS, cid, "/run/podman-init")
            if seen:
                self.starts[self.lives] = seq
        elif what == "run" and cid in self.running:
            number, _, _ = self.running[cid]
            child = args[1]  # the init's child (in the init tree), or podman exec (from conmon)
            self._ev(
                "process_exec", number, self._tick(), child, number if child else CONMON_NS, cid
            )
        elif what == "init" and cid in self.running:
            number, start, _ = self.running[cid]
            self._ev("process_kprobe", number, start, True, CONMON_NS, cid, "/run/podman-init")
        elif what == "stop" and cid in self.running:
            number, start, seen = self.running.pop(cid)
            child = self._tick()
            if seen:
                seq = self._ev(
                    "process_exit", number, start, True, CONMON_NS, cid, "/run/podman-init"
                )
                self.exits[self.cur[cid]] = seq
            self._ev("process_exit", number, child, True, number, cid)  # a child killed after it
            self.dead[cid] = (number, start, self.snaps)
        elif what == "late" and cid in self.dead and self.dead[cid][2] == self.snaps:
            number, start, _ = self.dead[cid]  # reordered — but never past a snapshot
            self.late.add(self._ev("process_kprobe", number, start, True, CONMON_NS, cid))
        elif what == "transient" and (number := self._free()) is not None:
            self._ev("process_exec", number, self._tick(), False, CONMON_NS, None, "/usr/bin/nft")
        elif what == "sandbox" and (number := self._free()) is not None:
            # the VM entering a short-lived namespace of its own (a sandboxed user unit), which
            # dies at once: its number is free again, with podman's entry still on record
            seq = len(self.recs)
            self.recs.append(_entry(seq, "b", _stamp(self._tick()), number))
            self.truth[seq] = None
        elif what == "podman":  # podman entering its namespace again: every podman command does
            seq = len(self.recs)
            self.recs.append(_entry(seq, "b", _stamp(self._tick()), CONMON_NS))
            self.truth[seq] = None
        elif what == "snap":
            self._snap()


def _entry(seq: int, boot: str, time: str, inode: int) -> dict:
    """podman entering its rootless namespace ``inode`` from the VM's own (a process there, its
    parent in the VM's mount namespace, outside the VM's user namespace) — how the join knows the
    namespace conmon starts containers from (:func:`foldyard.monitorstore.ns_fact`)."""
    proc = {"binary": "/usr/bin/conmon", "pid": 3, "uid": 0, "start_time": time,
            "ns": {"mnt": {"inum": inode}, "user": {"inum": 4026532575}}}  # fmt: skip
    parent = {"binary": "/usr/bin/podman", "ns": {"mnt": {"inum": 1, "is_host": True}}}
    return {"k": "ev", "boot": boot, "seq": seq,
            "e": {"process_exec": {"process": proc, "parent": parent}, "time": time}}  # fmt: skip


def _mnt(rec: dict) -> int:
    body = next(v for k, v in rec["e"].items() if k.startswith("process_"))
    return body["process"]["ns"]["mnt"]["inum"]


def _around(recs: list[dict], seq: int) -> tuple[dict | None, dict | None]:
    before = next((r for r in reversed(recs[:seq]) if r["k"] == "scopes"), None)
    after = next((r for r in recs[seq + 1 :] if r["k"] == "scopes"), None)
    return before, after


@settings(max_examples=scaled_examples(300), deadline=None)
@given(scenario=ns_steps)
# the brief's case: the old container's number handed straight on, no snapshot in between
@example(scenario=[("start", 0, True, False), ("snap",), ("run", 0, False), ("stop", 0),
                   ("start", 1, True, False), ("run", 1, False), ("snap",)])  # fmt: skip
# a container that came and went inside the window, an unmarked one next: neither gets the other's
@example(scenario=[("start", 0, True, False), ("snap",), ("stop", 0), ("start", 1, True, False),
                   ("run", 1, False), ("stop", 1), ("start", 2, False, False), ("snap",)])  # fmt: skip
# a late event of the dead container behind the new one's init (needs the process-start guard)
@example(scenario=[("start", 0, True, False), ("snap",), ("stop", 0),
                   ("start", 1, True, False), ("late", 0), ("snap",)])  # fmt: skip
def test_the_join_names_a_reused_namespace_only_by_its_own_container(scenario):
    world = NsWorld()
    for s in scenario:
        world.step(s)
    world._snap()
    events = {e.seq: e for e in monitorlog.attribute(world.recs, "fyex")}
    for seq, true in world.truth.items():
        e = events[seq]
        before, after = _around(world.recs, seq)
        b = (before or {}).get("map", {}).get(str(e.mnt))
        a = (after or {}).get("map", {}).get(str(e.mnt))
        # sound: where the two snapshots name different containers, only the true one, if any
        if b and a and b != a and e.who == "container":
            assert e.container == true, f"{seq} named {e.container[:1]}, it was {(true or '-')[:1]}"
        if seq not in world.life:
            continue
        cid, life = world.life[seq]
        late = seq in world.late
        # complete, the new holder: an event its own snapshot (the next) names it for, its init
        # seen entering — in this snapshot window or the one before — and alive when that snapshot
        # was taken (a restart under the same id inside one window reads as two containers: the
        # earlier life, over before the snapshot, is no one's the join can name)
        ended = world.exits.get(life)
        over = after is not None and ended is not None and ended < after["seq"]
        later = [q for other, q in world.starts.items() if other != life and q > world.starts.get(
            life, 1 << 30) and after is not None and q < after["seq"] and _mnt(world.recs[q]) == e.mnt]  # fmt: skip
        if a == [cid] and life in world.starts and not late and not later and not over:
            assert (e.who, e.container) == ("container", cid), f"{seq}: new holder lost"
        # complete, the old holder: an event before its init's exit, the snapshot before naming it
        # and not taken mid-setup (its own start mark would come after that snapshot)
        exit_seq = world.exits.get(life)
        start_seq = world.starts.get(life, -1)
        if (
            b == [cid]
            and exit_seq is not None
            and seq <= exit_seq
            and (before is not None and start_seq < before["seq"])
            and not any(r["k"] == "scopes" for r in world.recs[seq:exit_seq])
        ):
            assert (e.who, e.container) == ("container", cid), f"{seq}: old holder lost"


# ── no event given to a container outside its life ──────────────────────────────────────
#
# The follow-up (cabash, 2026-10-23, docs/monitor-pipeline.md §3): podman's netavark, nft and crun
# run for an instant in a mount namespace of their own, and the kernel gives it the lowest free
# number — the one a container is about to take, or has just left. A snapshot names the container
# for that number, and the join gave it podman's events from before its init entered or after it
# left: ~1,300 events a boot on cabash. The same world as above, held to the stronger claim: an
# event the join gives a container is that container's — unless Tetragon never marks its init, so
# nothing says where its life begins or ends, or a snapshot named it while the runtime was still
# setting it up (its init's exec after that snapshot in the stream: telling those apart needs the
# next window's marks, which a hook delivering at the first snapshot after can't wait for). Podman's
# tools run in the VM's pid namespace; a container's processes, the ones killed after its init
# included, in its own.
# Validated red: without the lifetime bounds (netavark before an init, the snapshot after naming
# the container; and after an init's exit, the snapshot before naming it — the first two
# @examples), and against each of (the @examples after, which random search finds only at tens of
# thousands of examples): no process-start guard; an exited life's processes taken as the
# container's in any pid namespace, or whichever life it was; the number free since inside the
# window read as free since before it; and a life that ended inside the window taken as the
# container's again because the snapshot after names it. What only completeness or the catalogue
# path depends on is pinned by unit tests (tests/test_monitorlog.py).


@settings(max_examples=scaled_examples(300), deadline=None)
@given(scenario=ns_steps)
# netavark on the number before the container's init entered, only the snapshot after naming it
@example(scenario=[("snap",), ("transient",), ("start", 0, True, False), ("snap",)])  # fmt: skip
# …and after its init left, only the snapshot before naming it
@example(scenario=[("start", 0, True, False), ("snap",), ("stop", 0), ("transient",),
                   ("snap",)])  # fmt: skip
# a late event of a container that came and went unseen, behind the next one's init (needs the
# process-start guard)
@example(scenario=[("start", 1, True, False), ("stop", 1), ("start", 0, False, False), ("stop", 0),
                   ("start", 1, False, False), ("late", 0)])  # fmt: skip
# the next container's processes after the old one's init left: only the old one's pid namespace
# is still the old one's, and only when the snapshot before saw that life
@example(scenario=[("start", 0, True, True), ("stop", 0), ("start", 1, True, False),
                   ("stop", 1)])  # fmt: skip
# …and the number free again inside the window: not the snapshot's container's either
@example(scenario=[("start", 0, True, False), ("snap",), ("stop", 0), ("start", 1, False, False),
                   ("stop", 1)])  # fmt: skip
# another container's whole life between a container's two: the snapshot after naming the first
# again doesn't make the middle one's events its
@example(scenario=[("start", 0, True, False), ("snap",), ("stop", 0), ("start", 1, True, False),
                   ("run", 1, False), ("stop", 1), ("start", 0, True, False), ("snap",)])  # fmt: skip
def test_no_event_is_given_to_a_container_outside_its_life(scenario):
    world = NsWorld()
    for s in scenario:
        world.step(s)
    world._snap()
    events = {e.seq: e for e in monitorlog.attribute(world.recs, "fyex")}
    for seq, true in world.truth.items():
        e = events[seq]
        if e.who != "container" or e.container == true:
            continue
        _, after = _around(world.recs, seq)
        named_early = after is not None and any(
            q > after["seq"] and _mnt(world.recs[q]) == e.mnt
            and world.life.get(q, ("", 0))[0] == e.container
            and not any(q2 for q2 in world.starts.values() if seq < q2 < after["seq"]
                        and _mnt(world.recs[q2]) == e.mnt)
            for q in world.starts.values()
        )  # fmt: skip
        if named_early:
            continue
        assert not world.marked.get(e.container, False), (
            f"{seq} ({'podman' if true is None else true[:1]}'s) given to {e.container[:1]}, "
            "outside its life"
        )


# ── a container's events never filed under what a hook skips ─────────────────────────────
#
# The review of #138: an `entry` has no end and numbers are reused lowest-free, so once a namespace
# the VM entered has died, a box container that takes its number — one Tetragon never shows an init
# for (`--pid=host`), gone before a snapshot names it — was read as podman's, which every hook
# without `who` skips (`monitorhooks.SKIP_UNLESS_ASKED`). The world above, with the VM entering a
# short-lived namespace on a free number now and then (`sandbox`), held to: no event a container
# caused is ever `podman` or `vm`. Validated red with an entry read as podman's for as long as
# nothing marked the number since (the first @example: an unmarked container's own process on it).

sandboxed_steps = st.lists(
    st.one_of(_ns_step, st.just(("sandbox",)), st.just(("podman",))), min_size=4, max_size=50
)


@settings(max_examples=scaled_examples(300), deadline=None)
@given(scenario=sandboxed_steps)
# an unmarked container on a number the VM entered once: its init's child
@example(scenario=[("sandbox",), ("start", 0, False, False), ("run", 0, True), ("snap",)])
# …and its processes killed after its init, gone before any snapshot
@example(scenario=[("sandbox",), ("start", 0, False, False), ("stop", 0)])
def test_a_containers_event_is_never_filed_under_what_hooks_skip(scenario):
    world = NsWorld()
    for s in scenario:
        world.step(s)
    world._snap()
    events = {e.seq: e for e in monitorlog.attribute(world.recs, "fyex")}
    for seq, true in world.truth.items():
        if true is not None:
            who = events[seq].who
            assert who not in mh.SKIP_UNLESS_ASKED, f"{seq} ({true[:1]}'s) filed under {who}"


# ── retention keeps what the join reads ─────────────────────────────────────────────────
#
# The catalogue keeps namespace facts from segments retention removed, so the events still stored
# are read as they were. The randomised runs on #138 found two ways it didn't: the running boot's
# facts deleted while the live segment held none of its records, and a kept init read without the
# fact before it that said where it came from (podman's earlier entry, dropped for a later one). The
# property: the namespace world above (with the VM's short-lived namespaces), stored in small
# segments and its oldest ones dropped as retention does; then ANY stretch of what is still stored,
# joined with the catalogue, reads every event exactly as the join over every record ever stored.
# Validated red against keeping only the newest fact of each kind about each namespace (the
# drop the PR first had), and against the join fetching only those.

retained = st.tuples(sandboxed_steps, st.integers(1, 6), st.integers(0, 40), st.integers(0, 40))


@settings(max_examples=scaled_examples(300), deadline=None)
@given(case=retained)
# a container's init exit kept, the entry into podman's namespace before it dropped for a later one:
# the exit then read as an init from nowhere, and podman's tool on the freed number went to the
# dead container
@example(case=([("start", 0, True, False), ("snap",), ("stop", 0), ("podman",), ("transient",),
                ("snap",)], 2, 0, 9))  # fmt: skip
def test_retention_keeps_every_fact_the_join_of_what_is_still_stored_reads(case):
    scenario, gone, start, length = case
    world = NsWorld()
    for s in scenario:
        world.step(s)
    world._snap()
    truth = {e.seq: e for e in monitorlog.attribute(world.recs, "fyex")}
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        mp.setattr(monitorstore.config, "state_dir", lambda: Path(tmp))
        mp.setattr(monitorstore, "SEGMENT_BYTES", 600)
        for i in range(0, len(world.recs), 3):  # the pull's appends: a few records at a time
            monitorstore.append(world.recs[i : i + 3])
        segs = monitorstore.segments()
        gone = min(gone, len(segs) - 1)
        with monitorstore.Catalogue.open() as cat:  # as retention does: the oldest files go
            cat.catch_up()
            for seg in segs[:gone]:
                seg.path.unlink()
            cat.drop([s.id for s in segs[:gone]])
        kept = [r for r, _ in monitorstore.scan(None)]  # a read: every record, not only events
        stretch = kept[start : start + max(length, 1)]
        if not any(r.get("k") == "ev" for r in stretch):
            return
        cat = monitorlog.catalogue()
        assert cat is not None
        try:
            got = monitorlog.join(stretch, cat, "fyex")
        finally:
            cat.close()
    for e in got:
        want = truth[e.seq]
        assert (e.who, e.container, e.crossed) == (want.who, want.container, want.crossed), (
            f"{e.seq}: {e.who} {e.container[:1]} after the drop, {want.who} {want.container[:1]} "
            "over everything"
        )
