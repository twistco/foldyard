"""Property-based tests (hypothesis) for the pure state-handling logic.

Two tiers here: metamorphic/round-trip properties over small pure functions
(supervisor.capability_edges, supervisor._osa_string, ports.project_base), and the
settle_incoherent invariants over GENERATED registries (tests/pbt.py) — the "plugin-agnostic"
claim tested against dependency shapes nobody has shipped.

Red/green convention for property tests: a property over existing code passes from day one,
which proves nothing by itself — before trusting one, sabotage the code under test (flip a
branch, drop an escape) and confirm hypothesis finds and shrinks a counterexample, then revert.
Each section notes the mutation it was validated against. When a property encodes an invariant
nothing yet guarantees and comes up red for real, that's a finding — fix the code, keep the
shrunk counterexample as an @example so it stays pinned.
"""

from __future__ import annotations

from unittest import mock

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from foldyard import devmode, ports, supervisor
from pbt import capability_maps, worlds

# ── supervisor.capability_edges — the probe-diff semantics ───────────────────────────────
# Validated red against: dropping the first-observation-failing branch (prev_ok is None), and
# inverting the flip comparison — both shrink to a one-axis map.


@given(current=capability_maps)
def test_capability_edges_quiet_on_identical_maps(current):
    # A stable world produces no edges — the "no duplicate notification every tick" guarantee.
    assert supervisor.capability_edges(current, current) == []


@given(previous=capability_maps, current=capability_maps)
def test_capability_edges_match_the_flip_spec(previous, current):
    # The full semantics, order-independently, against a declarative restatement: an edge per
    # (worktree, axis) in CURRENT whose merged verdict flipped — where a first observation
    # counts as a flip only when it is FAILING (booting into an active lapse must still say
    # so; booting into health is just normal).
    expected = set()
    for wt, axes in current.items():
        for axis, result in axes.items():
            prev = (previous.get(wt) or {}).get(axis)
            if prev is None:
                if not result["ok"]:
                    expected.add((wt, axis, False, result["detail"]))
            elif prev["ok"] != result["ok"]:
                expected.add((wt, axis, result["ok"], result["detail"]))
    got = supervisor.capability_edges(previous, current)
    assert len(got) == len(set(got)) and set(got) == expected


@given(previous=capability_maps)
def test_capability_edges_deactivation_is_not_a_heal(previous):
    # An axis (or whole worktree) that disappeared — rung back at default — is not an edge:
    # clearing published state must not fire a "recovered" notification.
    assert supervisor.capability_edges(previous, {}) == []


# ── supervisor._osa_string — AppleScript string-literal escaping ─────────────────────────
# Validated red against: dropping the backslash escape (shrinks to '\'), and skipping the
# quote escape (shrinks to '"').


def _osa_decode(literal: str) -> str:
    """Parse an AppleScript double-quoted string literal back to its value."""
    assert literal[0] == '"' and literal[-1] == '"'
    body, out, i = literal[1:-1], [], 0
    while i < len(body):
        if body[i] == "\\":
            out.append(body[i + 1])
            i += 2
        else:
            # An unescaped interior quote would terminate the literal early — probe details
            # carry quotes from gcloud error output, so this is the injection that matters.
            assert body[i] != '"'
            out.append(body[i])
            i += 1
    return "".join(out)


@given(s=st.text())
@example(s='say "hi" \\ bye')  # both escapes in one value, order-sensitive
def test_osa_string_round_trips(s):
    assert _osa_decode(supervisor._osa_string(s)) == s


# ── ports.project_base — cross-project band allocation ──────────────────────────────────
# Validated red against: dropping the `cand in taken` skip (bands collide on the second
# project) — shrinks to two projects.


def _fresh_port_registry():
    # The autouse isolated_port_registry fixture pins FY_PORTS_FILE per TEST; reset the file
    # per EXAMPLE so allocations from earlier examples can't exhaust the band space.
    ports.registry_file().unlink(missing_ok=True)


@given(
    projects=st.lists(
        st.text(alphabet="abcdefgh-", min_size=1, max_size=8), unique=True, min_size=1, max_size=8
    )
)
def test_port_bands_disjoint_aligned_and_stable(projects):
    _fresh_port_registry()
    bases = {p: ports.project_base(p) for p in projects}
    for base in bases.values():
        # In range and on a band boundary — a misaligned base would overlap a neighbour's
        # worktree-offset span even without an outright collision.
        assert ports.FIRST_BASE <= base <= ports.LAST_BASE
        assert (base - ports.FIRST_BASE) % ports.BAND == 0
    # Pairwise disjoint bands (bands are BAND-aligned, so distinct bases ⇒ disjoint ranges).
    assert len(set(bases.values())) == len(bases)
    # Stable: re-querying in any order re-reads the registry, never re-allocates. This is the
    # property the boxes rely on — the port is baked into box env at create time.
    for p in reversed(projects):
        assert ports.project_base(p) == bases[p]


@given(junk=st.text(max_size=64).filter(lambda t: "prj" not in t))
def test_port_allocation_survives_a_corrupt_registry(junk):
    # A truncated/hand-mangled ports.json must read as empty, not crash `fy up` — allocation
    # still returns a valid first band. (A VALID registry entry for the project is honoured
    # verbatim by design — manual edits are the documented pruning mechanism — hence the
    # filter keeping the project name out of the junk.)
    _fresh_port_registry()
    ports.registry_file().write_text(junk)
    base = ports.project_base("prj")
    assert ports.FIRST_BASE <= base <= ports.LAST_BASE
    assert (base - ports.FIRST_BASE) % ports.BAND == 0


# ── the declarative Requires evaluator (both tiers) vs the hook vs the oracle ────────────
# The shipped cross-plugin guards are declarative Requires DATA evaluated by
# Registry.mode_issues — in-code Axis.requires rows for intrinsic couplings
# (fakedep→fakecred) and consumer [[require]] config rows for wiring-dependent ones
# (llm→gcp, storage→gcp); the mode_issues hook remains the escape hatch. All three
# representations must agree with direct evaluation of the constraint data, for any
# generated world — incl. warn rows, empty accepts and self-referencing constraints. (The
# generated modes are TOTAL — every axis keyed — so the owner's missing-key-reads-as-default
# semantics is pinned by an example test in test_plugins.py, not here.) Validated red
# against: inverting the evaluator's `not in accepts`, and hardcoding severity to "error"
# (the warn rows in the generated constraints catch it); for the config tier additionally
# against dropping the [[require]] merge in Registry.__init__ and hardcoding the parsed
# rows' severity (the "config" source catches both).


@given(world=worlds())
def test_requires_evaluator_matches_the_hook_and_the_oracle(world):
    oracle = world.severities(world.mode)
    for source in ("hook", "axis", "config"):
        got = sorted(sev for sev, _ in world.registry(source).mode_issues(world.mode))
        assert got == oracle, source


@pytest.mark.parametrize("source", ["axis", "config"])
@given(world=worlds())
def test_requires_messages_carry_their_own_atomic_fix(world, source):
    # Pair each issue with ITS violated constraint (replicating the evaluator's iteration
    # order: axes in declaration order, requirement rows in constraint order) and assert the
    # synthesized message names THAT constraint's owner, value, and — when the requirement is
    # satisfiable — its atomic `fy mode` fix with the suggested (first-accepted) rung. The
    # per-row pairing matters: a renderer borrowing another row's fix must fail, not pass
    # against a pooled set. This is the message contract set_mode relies on ("include the
    # fix; de-escalation is never trapped").
    violated = []
    for ax in world.axes:
        value = world.mode.get(ax.name, ax.rungs[0])
        for axis, trigger, other, allowed, severity in world.constraints:
            if axis == ax.name and value in trigger and world.mode.get(other) not in allowed:
                violated.append((severity, ax.name, value, other, sorted(allowed)))
    issues = world.registry(source).mode_issues(world.mode)
    assert len(issues) == len(violated)
    for (sev, msg), (exp_sev, owner, value, other, allowed) in zip(issues, violated, strict=True):
        assert sev == exp_sev
        assert f"{owner}={value} needs" in msg
        if allowed:
            assert f"`fy mode {other}={allowed[0]} {owner}={value}`" in msg


# ── devmode.settle_incoherent — the expiry cascade over GENERATED registries ─────────────
# The plugin-agnostic claim, tested against synthetic worlds (tests/pbt.py): axes with 2-4
# rungs, requires-style constraints that may be unsatisfiable or error even at all-defaults —
# run over ALL THREE constraint representations (mode_issues hook, in-code Axis.requires,
# and config [[require]] rows), so settle's whole contract also exercises the core Requires
# evaluator — and the config parse/merge in front of it — end-to-end.
# Validated red against: settling to rungs[-1] instead of the default (downgrade-only shrinks
# to one two-rung axis), and accepting non-strict error decreases in the greedy loop (the
# stalled-input property catches the resulting pointless churn).

_STYLES = pytest.mark.parametrize("source", ["hook", "axis", "config"])


def _settle(world, mode, source="hook"):
    reg = world.registry(source)
    with mock.patch.object(devmode, "registry", lambda: reg):
        return devmode.settle_incoherent(dict(mode))


@_STYLES
@given(world=worlds())
def test_settle_downgrades_only_and_only_active_axes(world, source):
    flips = _settle(world, world.mode, source)
    defaults = world.defaults()
    # Every flip lands the axis AT its default (settling never raises a rung, never picks an
    # intermediate one), and only axes that were actually off their default flip at all.
    assert all(value == defaults[axis] for axis, value in flips.items())
    assert all(world.mode[axis] != defaults[axis] for axis in flips)


@_STYLES
@given(world=worlds())
def test_settle_never_increases_errors_and_keeps_coherent_modes_untouched(world, source):
    flips = _settle(world, world.mode, source)
    settled = {**world.mode, **flips}
    assert len(world.errors(settled)) <= len(world.errors(world.mode))
    if not world.errors(world.mode):
        assert flips == {}


@_STYLES
@given(world=worlds())
def test_settle_residual_errors_mean_even_all_defaults_errors(world, source):
    # settle's contract: an emergency lapse lands on a posture that WORKS offline. The only
    # excuse for leaving errors standing is a world where even everything-at-rest errors (a
    # misconfiguration, not a combination) — then nothing is flipped pointlessly.
    flips = _settle(world, world.mode, source)
    settled = {**world.mode, **flips}
    if world.errors(settled):
        assert world.errors(world.defaults())


@_STYLES
@given(world=worlds())
def test_settle_never_thrashes_a_stalled_mode(world, source):
    # When NO single downgrade strictly reduces the errors AND even all-defaults errors, settle
    # must flip NOTHING — leave the posture visible for `fy mode` rather than tear down axes
    # that don't help (the existing example test's "unfixable" case, generalized: this is what
    # pins the greedy loop's STRICT decrease).
    errors = world.errors(world.mode)
    defaults = world.defaults()
    stalled = errors and all(
        len(world.errors({**world.mode, axis: default})) >= len(errors)
        for axis, default in defaults.items()
        if world.mode[axis] != default
    )
    if stalled and world.errors(defaults):
        assert _settle(world, world.mode, source) == {}


@_STYLES
@given(world=worlds())
def test_settle_is_idempotent(world, source):
    # Settling a settled mode changes nothing — the supervisor may settle on every expiry
    # tick, so a non-idempotent settle would flap axes down tick after tick.
    flips = _settle(world, world.mode, source)
    assert _settle(world, {**world.mode, **flips}, source) == {}
