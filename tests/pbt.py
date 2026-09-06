"""Shared hypothesis strategies for the property-based tests (not a test module).

The centrepiece is the SYNTHETIC-REGISTRY world: generated axis sets + "requires"-style
coherence constraints, built into a real :class:`~foldyard.plugins.Registry`. Substrate code
(settle_incoherent, the reconcile model) is supposed to be plugin-AGNOSTIC — these strategies
are what actually test that claim, against dependency shapes nobody has shipped yet.

Constraints are generated as plain data (hypothesis shrinks data, not closures) and evaluated
four independent ways, so the tests can hold them against each other:
``(axis, trigger_rungs, other, allowed_rungs, severity)`` reads "while ``axis`` is at a rung
in ``trigger_rungs``, ``other`` must be at a rung in ``allowed_rungs``".

- :meth:`World.registry` (``source="hook"``) builds the registry with the constraints as a
  ``mode_issues`` HOOK (SyntheticPlugin) — the plugin-code escape-hatch path.
- ``source="axis"`` builds the SAME constraints as :class:`~foldyard.plugins.Requires` rows
  on the axes — the in-code data tier (fakedep→fakecred); this is what tests the core
  evaluator in ``Registry.mode_issues``.
- ``source="config"`` builds them as consumer ``[[require]]`` rows in a synthetic Config —
  the config tier the wiring-dependent guards (llm→gcp, storage→gcp) migrated to; this
  additionally exercises the parse + Registry-construction merge in front of that same
  evaluator.
- :meth:`World.errors`/:meth:`World.severities` evaluate the data DIRECTLY — the hand-rolled
  oracle no registry code touches.

The constraint shape covers every historical coherence case (llm=live requires gcp∈{sa,user})
and, because ``trigger_rungs`` may include the default and ``allowed_rungs`` may be empty,
also generates worlds that error at all-defaults — the settle "leave it visible" path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from hypothesis import strategies as st

from foldyard import config as config_mod
from foldyard.plugins import Axis, Plugin, Registry, Requires

# A constraint row: (axis, trigger_rungs, other_axis, allowed_rungs, severity) — an issue of
# `severity` while mode[axis] ∈ trigger_rungs and mode[other_axis] ∉ allowed_rungs.
Constraint = tuple[str, frozenset[str], str, frozenset[str], str]


class SyntheticPlugin(Plugin):
    """A registry-shaped world from generated data: axes + requires-style mode_issues."""

    name = "synthetic"

    def __init__(self, axes: list[Axis], constraints: list[Constraint]):
        self._axes = axes
        self._constraints = constraints

    def axes(self) -> list[Axis]:
        return list(self._axes)

    def mode_issues(self, mode: dict):
        for axis, trigger, other, allowed, severity in self._constraints:
            if mode.get(axis) in trigger and mode.get(other) not in allowed:
                yield (
                    severity,
                    f"{axis}∈{sorted(trigger)} requires {other}∈{sorted(allowed)}",
                )


@dataclass(frozen=True)
class World:
    """One generated consumer: its axes, constraints, a Registry, and an input mode."""

    axes: tuple[Axis, ...]
    constraints: tuple[Constraint, ...]
    mode: dict[str, str]

    def _plugin(self) -> SyntheticPlugin:
        return SyntheticPlugin(list(self.axes), list(self.constraints))

    def registry(self, source: str = "hook") -> Registry:
        """The world as a real Registry — the constraints represented as ``source`` says, so
        properties can run against EVERY evaluator path and hold them to the same oracle:
        ``"hook"`` (the ``mode_issues`` escape hatch), ``"axis"`` (in-code ``Axis.requires``
        rows), or ``"config"`` (consumer ``[[require]]`` rows in a synthetic Config, exercising
        the parse + construction-time merge in front of the same evaluator)."""
        if source == "hook":
            return Registry([self._plugin()])
        if source == "axis":
            by_axis: dict[str, list[Requires]] = {}
            for axis, trigger, other, allowed, severity in self.constraints:
                by_axis.setdefault(axis, []).append(
                    Requires(
                        when=tuple(sorted(trigger)),
                        axis=other,
                        accepts=tuple(sorted(allowed)),
                        severity=severity,
                    )
                )
            axes = [replace(ax, requires=tuple(by_axis.get(ax.name, ()))) for ax in self.axes]
            return Registry([SyntheticPlugin(axes, [])])
        if source == "config":
            rows = [
                {
                    "axis": axis,
                    "when": sorted(trigger),
                    "needs": other,
                    "accepts": sorted(allowed),
                    "severity": severity,
                }
                for axis, trigger, other, allowed, severity in self.constraints
            ]
            cfg = config_mod.Config(
                repo_root=config_mod.repo_root(), worktree="", toml={"require": rows}
            )
            return Registry([SyntheticPlugin(list(self.axes), [])], config=cfg)
        raise ValueError(f"unknown constraint source {source!r}")

    def defaults(self) -> dict[str, str]:
        return {ax.name: ax.default for ax in self.axes}

    def severities(self, mode: dict) -> list[str]:
        """DIRECT evaluation of the constraint data (the oracle — no registry/plugin code):
        the sorted severities of every violated constraint."""
        return sorted(
            severity
            for axis, trigger, other, allowed, severity in self.constraints
            if mode.get(axis) in trigger and mode.get(other) not in allowed
        )

    def errors(self, mode: dict) -> list[str]:
        """The oracle's error rows only (what gates set_mode and drives settle)."""
        return [sev for sev in self.severities(mode) if sev == "error"]


@st.composite
def worlds(draw, max_axes: int = 5, max_constraints: int = 6) -> World:
    """A synthetic consumer + an input mode over its axes (any rung, incl. defaults)."""
    n_axes = draw(st.integers(min_value=1, max_value=max_axes))
    axes: list[Axis] = []
    for i in range(n_axes):
        n_rungs = draw(st.integers(min_value=2, max_value=4))
        rungs = tuple(f"r{j}" for j in range(n_rungs))
        axes.append(Axis(name=f"ax{i}", rungs=rungs, blurb=dict.fromkeys(rungs, "-")))
    constraints: list[Constraint] = []
    for _ in range(draw(st.integers(min_value=0, max_value=max_constraints))):
        axis = draw(st.sampled_from(axes))
        trigger = draw(st.frozensets(st.sampled_from(axis.rungs), min_size=1))
        other = draw(st.sampled_from(axes))
        # allowed may be EMPTY (unsatisfiable while trigger holds) and may or may not contain
        # the other axis's default — both matter for settle's fallback paths. Error-biased:
        # warns exercise the severity path but never gate/settle anything.
        allowed = draw(st.frozensets(st.sampled_from(other.rungs)))
        severity = draw(st.sampled_from(["error", "error", "warn"]))
        constraints.append((axis.name, trigger, other.name, allowed, severity))
    mode = {ax.name: draw(st.sampled_from(ax.rungs)) for ax in axes}
    return World(axes=tuple(axes), constraints=tuple(constraints), mode=mode)


# ── capability maps (supervisor.capability_edges inputs) ─────────────────────────────────
# Well-formed probe results as run_capability_probes publishes them: every entry carries a
# boolean "ok" (capability_edges treats a MISSING ok as falsy-first-observation, which only
# malformed data produces — the generator stays within the published contract).

_WORKTREES = st.sampled_from(["", "wt-a", "wt-b"])
_AXES = st.sampled_from(["gcp", "github", "llm"])

capability_results = st.fixed_dictionaries({"ok": st.booleans(), "detail": st.text(max_size=12)})

capability_maps = st.dictionaries(
    _WORKTREES,
    st.dictionaries(_AXES, capability_results, max_size=3),
    max_size=3,
)
