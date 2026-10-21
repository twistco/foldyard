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

Validated red against: the code before the fix (shrinks to one engine claim, a snapshot and three
events read a window apart — names lost, nothing in ``gaps``); after it, against: settling on age
while a later snapshot exists (the old ``settled``), joining without the catalogue's snapshots
around the read, and dropping the ``rotated`` gap — each shrinks to a handful of steps.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from hypothesis import event, given, settings
from hypothesis import strategies as st

from foldyard import monitorhooks as mh
from foldyard import monitorlog, monitorstore

A, B, C = "a" * 64, "b" * 64, "c" * 64
META = {
    A: {"name": "fyex-api-1", "project": "fyex", "service": "api"},
    B: {"name": "fyex-feat-devbox", "project": "", "service": ""},
    C: None,  # gone before the host asked
}
INODES = (1, 200, 300, 400)  # 1 is the VM's own namespace
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
        st.tuples(st.just("ev"), st.sampled_from(INODES), st.sampled_from(KINDS)),
        st.tuples(st.just("burst"), st.integers(5, 40), st.sampled_from(INODES)),
        st.tuples(st.just("snap"), mappings),
        st.tuples(st.just("engine"), st.sampled_from([A, B, C])),
        st.tuples(st.just("lost"), st.integers(1, 3)),
        st.just(("relaygap",)),
        st.just(("boot",)),
        st.just(("pull",)),
        st.just(("round",)),
        st.tuples(st.just("wait"), st.sampled_from([1.0, 30.0, 200.0])),
    ),
    min_size=8,
    max_size=60,
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

    def _rec(self, body: dict) -> None:
        self.pending.append({**body, "boot": self.boot, "seq": self.seq})
        self.seq += 1

    def step(self, s: tuple) -> None:
        what = s[0]
        if what == "ev":
            proc = {"binary": f"/bin/b{s[1]}", "pid": 7, "uid": 1, "ns": {"mnt": {"inum": s[1]}}}
            self._rec({"k": "ev", "e": {s[2]: {"process": proc}, "time": _iso(self.now)}})
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


@settings(max_examples=150, deadline=None)
@given(scenario=steps, filters=hooks, budget=st.sampled_from(["tiny", "ample"]))
def test_a_hook_delivers_the_full_store_join_or_says_what_it_could_not(
    monkeypatch, scenario, filters, budget
):
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp)
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
        for i, (who, kinds, worktrees) in enumerate(filters):
            hook = mh.Hook(name=f"h{i}", command=("/bin/h",), who=tuple(who), kinds=tuple(kinds),
                           worktrees=tuple(worktrees))  # fmt: skip
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
        rank = _order(world)
        event(f"records>={min(len(world.all) // 50 * 50, 200)}")
        for hook in made:
            batches = got.get(hook.name, [])
            gaps = [g for b in batches for g in b.get("gaps", [])]
            delivered: dict[tuple[str, int], dict] = {}
            for b in batches:
                for e in b["events"]:
                    key = (e["boot"], e["seq"])
                    assert delivered.setdefault(key, e) == e, "a re-sent event changed"
            want = {k: e for k, e in truth.items() if hook.matches(monitorlog.Event(**e))}
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
            for key in delivered:
                if key not in want:
                    assert _covered(key, gaps, rank, ("unsettled",)), (
                        f"{hook.name} got {key}, which its filter doesn't match in the full join"
                    )
            event(f"delivered>0={bool(delivered)}")
            event(f"whys={sorted({g['why'] for g in gaps})}")
            if budget == "ample":
                assert not [g for g in gaps if g["why"] == "rotated"], "rotated, with room to spare"
