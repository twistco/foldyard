"""Model-based stateful test of the mode/state control loop (hypothesis RuleBasedStateMachine).

The audited failure history (docs/mode-state-consolidation.md) is SEQUENCES — mode set, TTL
lapse, probe flip, box up/down, supervisor restart — landing the tiers in a combination nobody
enumerated. The conformance suite (test_reconcile_scenarios.py) pins the sequences someone
already thought of; this machine generates the rest: random interleavings of the real
operations, each checked against a small pure-python model of what the posture tiers should say.

What runs is the REAL code on real state files in a temp dir: ``devmode.set_mode`` (the
coherence gate + the mirror refresh), ``supervisor.expire_user_modes`` (TTL expiry + the settle
cascade), ``run_capability_probes`` → ``_advance_capability_baseline`` → ``write_capabilities``
→ ``_react_to_capability_edges`` and the box-up mirror write — the exact per-worktree tick loop
from ``reconcile_once`` (``for wt in up_worktrees() or [""]`` under ``config.using`` of each
worktree's config) — with the clock driven through the ``fy clock`` skew env. The world is a
fixed two-axis synthetic consumer — an identity ladder with an emergency rung and a dependent
axis, the minimal shape with every interaction the model checks (the varied-registry generation
lives in test_properties.py) — replicated across TWO checkouts: main (``""``) and one worktree,
each with its own posture file, TTLs, and mirror, sharing one supervisor, one capability
baseline, and one external capability chain (the probe verdict — a PAM grant doesn't care which
checkout asks).

The MIRROR tier (failure class 1 in the doc — two tiers disagreeing about one fact) is modeled
per worktree: ``box_up``/``box_down`` rules flip what ``up_worktrees`` reports, the tick and
``set_mode`` write/refresh each checkout's mirror exactly as the real triggers do, and each
box-side view (``devmode.read()`` under ``in_box``, bound to that worktree's config) is checked
against the model after every step — including the mirror-creation discipline (a tick or
set_mode with that box down must never seed a mirror into a clean checkout, bug #44) and the
capability slice a ``set_mode`` refresh deliberately drops until the next tick re-stamps it.

Faithful-by-construction caveats worth knowing (the machine REPLICATES reconcile_once's
orchestration, so it pins these rather than judging them): with any box up, the tick loop
processes only the UP worktrees — a down checkout's TTL expiry stays read-applied (never
durably settled) and its axes drop out of the published capability map until it is processed
again; both are the real loop's semantics.

Deliberate scope cuts, covered elsewhere: daemon lifecycle (unit tests on _child_step/
_spawn_child), the compose stack edge (golden tests; reconcile_stack is stubbed here), and the
resnapshot restart itself (the heal edge is asserted; the thread body is unit-tested).

Red/green: validated against mutations of the code under test — see each invariant's note in
git history / test_properties.py's convention docstring.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from foldyard import config, devmode, supervisor
from foldyard.plugins import Axis, CapabilityProbe, Plugin, Registry, Requires

# The synthetic world: an identity ladder (emergency top rung → TTL-bound) + a dependent axis.
# dep=live requires ident∈{sa,user} — the llm-live-under-gcp shape from the incident history.
DEFAULTS = {"ident": "off", "dep": "off"}
RUNG_CHOICES = [
    ("ident", "off"),
    ("ident", "sa"),
    ("ident", "user"),
    ("dep", "off"),
    ("dep", "live"),
]
# The checkouts: main + one worktree, each its own posture/mirror state under one supervisor.
WORKTREES = ["", "wt-a"]
# TTLs and clock jumps chosen so no sum of jumps ever lands within seconds of a TTL boundary —
# the model and the code read the clock microseconds apart, so exact-boundary races must be
# unreachable by construction (margins are ≥200s).
TTLS = [600, 7000]
CLOCK_JUMPS = [1800, 10800]

_ENV_KEYS = ("FOLDYARD_CAPABILITIES_FILE", "FOLDYARD_STATE_DIR", "FOLDYARD_CLOCK_OFFSET")


def _errors(mode: dict) -> list[str]:
    if mode.get("dep") == "live" and mode.get("ident") not in ("sa", "user"):
        return ["dep=live requires ident — fy mode ident=sa dep=live"]
    return []


class ModeStateModel(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.tmp = Path(tempfile.mkdtemp(prefix="fy-mode-model-"))
        self.notifications: list[str] = []
        machine = self

        class WorldPlugin(Plugin):
            name = "world"

            def axes(self):
                return [
                    Axis(
                        name="ident",
                        rungs=("off", "sa", "user"),
                        blurb=dict.fromkeys(("off", "sa", "user"), "-"),
                        emergency=("user",),
                    ),
                    Axis(
                        name="dep",
                        rungs=("off", "live"),
                        blurb={"off": "-", "live": "-"},
                        # DECLARATIVE (Axis.requires, not a mode_issues hook) so the machine
                        # drives the same core evaluator the shipped guards use, end to end
                        # through set_mode's gate and the expiry's settle. `_errors` above
                        # stays the model's independent oracle for the same constraint.
                        requires=(Requires(when=("live",), axis="ident", accepts=("sa", "user")),),
                    ),
                ]

            def capability_probes(self, mode):
                if mode.get("ident", "off") == "off":
                    return []
                return [
                    CapabilityProbe(
                        axis="ident",
                        name="fake-ident",
                        check=lambda: (machine.probe_ok, "probed"),
                        interval=0.0,  # always due — the tick cadence is the machine's to drive
                    )
                ]

        reg = Registry([WorldPlugin()])
        # One resolved Config per checkout — what the real tick binds per worktree. The toml is
        # irrelevant (the registry is patched); the WORKTREE key is what the bound path/state
        # functions resolve against.
        self._cfgs = {
            wt: config.Config(
                repo_root=config.repo_root(), worktree=wt, toml={"project": {"name": "model"}}
            )
            for wt in WORKTREES
        }
        self._saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["FOLDYARD_CAPABILITIES_FILE"] = str(self.tmp / "capabilities.json")
        os.environ["FOLDYARD_STATE_DIR"] = str(self.tmp)
        os.environ["FOLDYARD_CLOCK_OFFSET"] = "0"

        def _wt() -> str:
            return config.active_worktree() or "main"

        self._patches = [
            # Worktree-AWARE state paths: whichever config is bound (the tick's per-worktree
            # binding, the rules' own) resolves to that checkout's files — the real layout,
            # where each checkout carries its own dev-mode.json and repo mirror.
            mock.patch.object(config, "mode_file", lambda: self.tmp / f"dev-mode-{_wt()}.json"),
            mock.patch.object(config, "mirror_file", lambda: self.tmp / f"mirror-{_wt()}.json"),
            mock.patch.object(config, "host_env_file", lambda: self.tmp / "host.env"),
            mock.patch.object(devmode, "in_box", lambda: False),
            # The box lifecycle: what up_worktrees reports drives BOTH mirror-write gates
            # (set_mode's create-only-while-up, the tick's up-worktrees-only loop).
            mock.patch.object(devmode, "up_worktrees", lambda: sorted(self.up)),
            mock.patch.object(devmode, "worktree_config", lambda wt: self._cfgs[wt]),
            mock.patch.object(devmode, "reconcile_stack", lambda *a, **k: True),
            mock.patch.object(devmode, "registry", lambda: reg),
            mock.patch.object(
                supervisor, "_notify", lambda title, body: self.notifications.append(title)
            ),
        ]
        # Start the patches unwind-safely: hypothesis only calls teardown() on instances that
        # CONSTRUCT successfully, so a mid-loop start failure (e.g. a renamed patch target)
        # must not strand the earlier patches + clobbered env on the whole pytest process.
        self._started: list = []
        try:
            for p in self._patches:
                p.start()
                self._started.append(p)
        except Exception:
            self.teardown()
            raise
        supervisor._published_caps = None
        supervisor._probe_state.clear()

        # ── the model ──────────────────────────────────────────────────────────────────
        self.offset = 0  # seconds of fy-clock skew applied
        self.probe_ok = True  # the ONE external capability chain (shared across checkouts)
        self.up: set[str] = set()  # worktree keys whose dev box is up
        self.raw = {wt: dict(DEFAULTS) for wt in WORKTREES}  # each checkout's FILE (pre-expiry)
        self.deadline: dict[str, dict[str, datetime]] = {wt: {} for wt in WORKTREES}
        self.baseline_ok: dict[str, dict[str, bool]] = {}  # published verdicts {wt: {axis: ok}}
        self.file_caps: dict | None = None  # capabilities.json content (None = absent)
        # The MIRROR tier, per checkout: None = no mirror file (clean checkout). Otherwise the
        # content of the last write: the mode + the expiry deadlines it carried, and the
        # capability slice — None after a set_mode refresh (write_mirror(capabilities=None)
        # drops the key), the merged map after a box-up tick.
        self.mirror: dict[str, dict | None] = dict.fromkeys(WORKTREES)

    def teardown(self):
        while self._started:
            self._started.pop().stop()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        supervisor._published_caps = None
        supervisor._probe_state.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── model helpers ─────────────────────────────────────────────────────────────────
    def _now(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self.offset)

    def _live(self, wt: str) -> dict:
        """The expiry-applied view of checkout ``wt``'s file state — what read() should say."""
        now = self._now()
        deadline = self.deadline[wt]
        return {
            axis: DEFAULTS[axis] if axis in deadline and deadline[axis] <= now else value
            for axis, value in self.raw[wt].items()
        }

    def _merged_caps(self, live: dict) -> dict[str, bool]:
        """axis → merged probe verdict the tick should compute for one checkout's live mode."""
        return {} if live["ident"] == "off" else {"ident": self.probe_ok}

    def _set(self, wt: str, axis: str, rung: str, ttl: int) -> None:
        live = self._live(wt)
        prospective = {**live, axis: rung}
        with config.using(self._cfgs[wt]):
            if _errors(prospective):
                # The coherence gate: an error combination is refused atomically — no write.
                with pytest.raises(SystemExit):
                    devmode.set_mode({axis: rung}, ttl=ttl, reconcile=False)
                return
            devmode.set_mode({axis: rung}, ttl=ttl, reconcile=False)
        # set_mode reads with expiry applied and writes that back durably: lapsed rungs land
        # at their default and their expires entries drop, alongside the requested update.
        now = self._now()
        self.deadline[wt] = {a: d for a, d in self.deadline[wt].items() if d > now and a != axis}
        if rung == "user":  # the world's only emergency rung
            self.deadline[wt][axis] = now + timedelta(seconds=ttl)
        self.raw[wt] = prospective
        # set_mode REFRESHES an existing mirror but CREATES one only while that checkout's box
        # is up — and its refresh carries no capability slice (that's the tick's to stamp).
        if self.mirror[wt] is not None or wt in self.up:
            self.mirror[wt] = {
                "mode": dict(self.raw[wt]),
                "expires": dict(self.deadline[wt]),
                "caps": None,
            }

    # ── rules: the operations an operator/the clock/the world can perform ─────────────
    # Depth boost: most historical incidents START from an escalated posture (an emergency
    # identity rung carrying a dependent axis), so a plain uniform walk wastes most of its
    # steps getting there — the settle-cascade mutation went UNCAUGHT at 30 examples and
    # needed ~500 with a cold start. Seeding some runs at interesting coherent postures
    # (through the real set_mode path, so nothing is faked) and box states puts the deep
    # sequences within a small CI budget; calibration is re-checked by the noted mutations.
    @initialize(
        wt=st.sampled_from(WORKTREES),
        posture=st.sampled_from(
            [(), (("ident", "sa"),), (("ident", "user"), ("dep", "live"))],
        ),
        ttl=st.sampled_from(TTLS),
        up=st.sampled_from([(), ("",), ("", "wt-a"), ("wt-a",)]),
        probe=st.booleans(),
    )
    def init_posture(self, wt, posture, ttl, up, probe):
        self.up = set(up)
        self.probe_ok = probe  # some runs start mid-lapse — asymmetric probe histories need it
        for axis, rung in posture:
            self._set(wt, axis, rung, ttl)

    @rule(
        wt=st.sampled_from(WORKTREES),
        axis_rung=st.sampled_from(RUNG_CHOICES),
        ttl=st.sampled_from(TTLS),
    )
    def set_rung(self, wt, axis_rung, ttl):
        axis, rung = axis_rung
        self._set(wt, axis, rung, ttl)

    @rule(wt=st.sampled_from(WORKTREES))
    def box_up(self, wt):
        # `fy up`: that checkout's box exists from here on. Nothing writes its mirror at this
        # instant — the real supervisor seeds it "within a tick of box-up", i.e. our next tick().
        self.up.add(wt)

    @rule(wt=st.sampled_from(WORKTREES))
    def box_down(self, wt):
        # `fy down`: the mirror FILE stays behind on the shared mount and simply stops
        # refreshing (set_mode still refreshes an existing one; the tick no longer touches it).
        self.up.discard(wt)

    @rule(seconds=st.sampled_from(CLOCK_JUMPS))
    def advance_clock(self, seconds):
        self.offset += seconds
        os.environ["FOLDYARD_CLOCK_OFFSET"] = str(self.offset)

    @rule(ok=st.booleans())
    def flip_probe(self, ok):
        self.probe_ok = ok

    @rule()
    def restart_supervisor(self):
        # A new supervisor process: in-memory baseline and probe cache gone; the published
        # capabilities FILE survives and re-seeds the baseline — the lapse+heal-across-restart
        # guarantee under test.
        supervisor._published_caps = None
        supervisor._probe_state.clear()
        self.baseline_ok = {
            wt: {axis: bool(result["ok"]) for axis, result in axes.items()}
            for wt, axes in (self.file_caps or {}).items()
        }

    @rule()
    def tick(self):
        up = sorted(self.up)
        processed = up or [""]  # reconcile_once's loop: up boxes, or the main fallback
        capabilities: dict[str, dict] = {}
        merged_by_wt: dict[str, dict[str, bool]] = {}
        notes_before = len(self.notifications)

        for wt in processed:
            # ── model prediction for this checkout, before running its slice ─────────
            live = self._live(wt)
            expired = [a for a in self.raw[wt] if live[a] != self.raw[wt][a]]
            settle = {"dep": "off"} if expired and _errors(live) else {}
            expected_mode = {**live, **settle}
            merged_by_wt[wt] = self._merged_caps(expected_mode)

            # ── the real per-worktree slice, exactly as reconcile_once runs it ────────
            with config.using(self._cfgs[wt]):
                mode = supervisor.expire_user_modes()
                capabilities[wt] = supervisor.run_capability_probes(wt, mode)
                if wt in up:
                    devmode.write_mirror(
                        mode,
                        devmode.read()["expires"],
                        devmode.daemon_status(mode),
                        capabilities[wt],
                    )
                # Expiry lands coherent + durable, per checkout.
                assert mode == expected_mode
                durable = devmode.read(apply_expiry=False)
                assert durable["mode"] == expected_mode  # settled DURABLY, not just on read
                assert not _errors(durable["mode"])

            # ── advance this checkout's model state ───────────────────────────────────
            self.raw[wt] = expected_mode
            now = self._now()
            self.deadline[wt] = {a: d for a, d in self.deadline[wt].items() if d > now}
            if wt in self.up:
                # A box-up tick stamps the full mirror content, capability slice included —
                # subsuming any refresh the expiry's forced set_mode just did.
                self.mirror[wt] = {
                    "mode": dict(expected_mode),
                    "expires": dict(self.deadline[wt]),
                    "caps": dict(merged_by_wt[wt]),
                }
            elif expired and self.mirror[wt] is not None:
                # Box down: the expiry's set_mode still refreshes an EXISTING (stale) mirror —
                # mode + expires only, the capability slice dropped until a box-up tick.
                self.mirror[wt] = {
                    "mode": dict(expected_mode),
                    "expires": dict(self.deadline[wt]),
                    "caps": None,
                }

        # ── the shared (cross-worktree) tail of the tick ──────────────────────────────
        expected_edges = set()
        for wt, merged in merged_by_wt.items():
            prev_axes = self.baseline_ok.get(wt, {})
            for axis, ok in merged.items():
                prev = prev_axes.get(axis)
                if (prev is None and not ok) or (prev is not None and prev != ok):
                    expected_edges.add((wt, axis, ok))

        edges = supervisor._advance_capability_baseline(capabilities)
        supervisor.write_capabilities(capabilities)
        supervisor._react_to_capability_edges(edges)

        # Edges fire exactly on merged-verdict flips, per (worktree, axis) independently.
        assert len(edges) == len(expected_edges)
        assert {(wt, a, ok) for wt, a, ok, _ in edges} == expected_edges
        # One notification per edge — a stable world must stay silent (no per-tick re-nag),
        # every flip must say so (DEGRADED on lapse, recovered on heal), naming its worktree.
        notes = self.notifications[notes_before:]
        assert len(notes) == len(edges)
        for (wt, axis, ok, _), title in zip(edges, notes, strict=True):
            assert ("recovered" if ok else "DEGRADED") in title and axis in title
            assert (f"worktree {wt}" in title) == bool(wt)

        # The published tier is REPLACED wholesale: an unprocessed checkout drops out of both
        # the baseline and the file (its axes make no claim until it is processed again).
        self.baseline_ok = {wt: dict(m) for wt, m in merged_by_wt.items()}
        if any(capabilities.values()) or self.file_caps is not None:
            self.file_caps = capabilities

    # ── invariants: the tiers agree, per checkout, after EVERY operation ──────────────
    @invariant()
    def read_view_matches_the_model(self):
        # devmode.read (the tier every dashboard consumes) always equals the model's
        # expiry-applied view of THAT checkout — whatever sequence led here.
        for wt in WORKTREES:
            with config.using(self._cfgs[wt]):
                assert devmode.read()["mode"] == self._live(wt)

    @invariant()
    def expires_entries_only_on_emergency_rungs(self):
        # No checkout's file ever carries an expiry for an axis not actually holding an
        # emergency rung (set_mode pops it on de-escalation; expiry pops it on lapse).
        for wt in WORKTREES:
            with config.using(self._cfgs[wt]):
                durable = devmode.read(apply_expiry=False)
            for axis in durable["expires"]:
                assert durable["mode"][axis] == "user"

    @invariant()
    def mirror_exists_only_when_the_model_says(self):
        # A clean checkout STAYS clean: nothing may seed a mirror while that box is down (bug
        # #44 — the idle supervisor re-dirtying checkouts); and once written, box-down leaves
        # the file behind (it goes stale, it doesn't vanish).
        for wt in WORKTREES:
            with config.using(self._cfgs[wt]):
                exists = config.mirror_file().exists()
            assert exists == (self.mirror[wt] is not None)

    @invariant()
    def box_view_matches_the_model(self):
        # The tier-disagreement check (failure class 1): what a BOX session in each checkout
        # reads — its mirror via devmode.read, expiry applied against the MIRROR's own expires
        # — must equal the model's prediction from the last mirror write. This pins both the
        # agreement (every authoritative write reached the mirror it should have) and the
        # DELIBERATE divergences: a set_mode refresh drops the capability slice until the next
        # tick re-stamps it, and a down checkout's mirror goes stale rather than tracking.
        for wt in WORKTREES:
            with config.using(self._cfgs[wt]), mock.patch.object(devmode, "in_box", lambda: True):
                seen = devmode.read()
            mirror = self.mirror[wt]
            if mirror is None:
                assert seen["mode"] == DEFAULTS and seen["capabilities"] is None
                continue
            now = self._now()
            expected = {
                axis: DEFAULTS[axis]
                if axis in mirror["expires"] and mirror["expires"][axis] <= now
                else value
                for axis, value in mirror["mode"].items()
            }
            assert seen["mode"] == expected
            if mirror["caps"] is None:
                assert seen["capabilities"] is None
            else:
                assert {
                    axis: bool(result["ok"])
                    for axis, result in (seen["capabilities"] or {}).items()
                } == mirror["caps"]

    @invariant()
    def published_capabilities_match_the_model(self):
        path = config.capabilities_file()
        if self.file_caps is None:
            assert not path.exists()
        else:
            published = json.loads(path.read_text())
            assert set(published) == set(self.file_caps)
            for wt, axes in self.file_caps.items():
                assert {a: r["ok"] for a, r in published[wt].items()} == {
                    a: r["ok"] for a, r in axes.items()
                }


TestModeStateModel = ModeStateModel.TestCase
TestModeStateModel.settings = settings(max_examples=60, stateful_step_count=40, deadline=None)
