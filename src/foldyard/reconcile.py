"""The reconciler scope contract — every state tier in ONE inventory (proposal A).

Posture state lives on tiers with different refresh lifetimes, and the audited bug history
(docs/mode-state-consolidation.md) is mostly "two tiers disagreeing about one fact". This
module is the consolidation: each tier is a :class:`Scope` that declares how to DESCRIBE
desired vs observed (``rows`` — what ``fy state`` renders) and, where the scope can act,
how to CONVERGE (``reconcile``/``publish`` — invoked from the tier's existing trigger).
A new tier = one new class here, and it appears in ``fy state`` and gets its trigger wiring
in the same review.

Adapters, not moves — deliberately. Each action delegates to the existing, golden-tested
implementation (``stack.reconcile_posture``, the supervisor tick helpers), so the logic
stays next to its tests and its trigger; what this contract consolidates is the INVENTORY
(tier-of-truth explicit, view and action co-located so they cannot drift apart) — see the
conformance suite (tests/test_reconcile_scenarios.py), which pins what fires when.

| scope      | trigger                    | action (delegate)                              |
|------------|----------------------------|------------------------------------------------|
| posture    | `fy mode` write            | devmode.set_mode is the writer (rows only)     |
| daemons    | supervisor tick (~2s)      | supervisor._child_step/_spawn_child            |
| capability | supervisor tick (due only) | supervisor.run_capability_probes + publish     |
| stack      | mode change (set_mode)     | stack.reconcile_posture (signature edge)       |
| assets     | `fy up` + mode change      | registry().stage_assets — an idempotent copy,  |
|            |                            | self-converging at its triggers (no rows: the  |
|            |                            | staging has no observation hook to render)     |
| box env    | `fy box up` (observe-only) | can't act — devmode._box_env_hint nags         |

Stdlib only (loads on the `fy state` path).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from . import config, devmode


@dataclass(frozen=True)
class ScopeRow:
    """One desired → observed line: ``status`` is ``ok`` (agree), ``drift`` (disagree — the
    observed text carries the fix), or ``unknown`` (can't observe from here; never guessed)."""

    status: str
    scope: str
    desired: str
    observed: str


class Scope:
    """One reconciled state tier. ``rows(state)`` describes desired vs observed for the
    dashboards (``state`` is ``devmode.read()``'s dict); action methods, where the scope has
    any, are invoked by the tier's trigger (see the module table). ``host_only`` marks scopes
    whose observation needs the host (the box skips them)."""

    name = ""
    host_only = False

    def rows(self, state: dict) -> list[ScopeRow]:
        return []


def _mode_summary(mode: dict) -> str:
    defaults = devmode.axis_defaults()
    active = [f"{a}={v}" for a, v in mode.items() if v != defaults.get(a)]
    return " ".join(active) if active else "fully offline (all defaults)"


class PostureScope(Scope):
    """The posture files themselves — the desired tier. ``devmode.set_mode`` is its writer
    (rows only here): host-side the authoritative file is current by definition; in the box
    the mirror is the readable tier and staleness means the supervisor isn't heartbeating.
    Standing incoherence (a pre-settle force-write) surfaces as drift."""

    name = "posture"

    def rows(self, state: dict) -> list[ScopeRow]:
        written = devmode._parse(state.get("written") or "")
        age = int((devmode.now() - written).total_seconds()) if written else None
        desired = _mode_summary(state["mode"])
        issues = [
            msg for sev, msg in devmode.registry().mode_issues(state["mode"]) if sev == "error"
        ]
        if issues:
            return [ScopeRow("drift", self.name, desired, f"incoherent: {issues[0]}")]
        if devmode.in_box():
            if age is None or age > 15:
                shown = age if age is not None else "?"
                return [
                    ScopeRow(
                        "drift",
                        self.name,
                        desired,
                        f"mirror stale ({shown}s) — is `fy host` running?",
                    )
                ]
            return [ScopeRow("ok", self.name, desired, f"mirror fresh ({age}s old)")]
        return [ScopeRow("ok", self.name, desired, f"authoritative ({state['source']})")]


class DaemonScope(Scope):
    """Host daemons vs the posture's desired set. The action is the supervisor tick
    (``_child_step``/``_spawn_child`` convergence, orphan reaping); here: a live TCP probe
    per desired daemon (host) or the supervisor's heartbeat status (box mirror). Fully
    offline, a down daemon is a note, not drift — nothing flows through it until a rung
    rises (`fy up` starts it with the stack)."""

    name = "daemons"

    def rows(self, state: dict) -> list[ScopeRow]:
        mode = state["mode"]
        wanted = devmode.desired_daemons(mode)
        if not wanted:
            return [ScopeRow("ok", self.name, "none (posture demands no daemon)", "—")]
        daemons = (state.get("daemons") or {}) if devmode.in_box() else devmode.daemon_status(mode)
        offline = all(v == devmode.axis_defaults().get(a) for a, v in mode.items())
        rows = []
        for name, spec in wanted.items():
            seen = daemons.get(name) or {}
            up = seen.get("up")
            desired = f"{name} listening on :{spec['port']}"
            if up:
                rows.append(ScopeRow("ok", self.name, desired, "up"))
            elif up is None:
                rows.append(
                    ScopeRow("unknown", self.name, desired, "no status — `fy host` running?")
                )
            elif offline:
                rows.append(
                    ScopeRow(
                        "unknown", self.name, desired, "not running (offline — `fy up` starts it)"
                    )
                )
            else:
                rows.append(
                    ScopeRow("drift", self.name, desired, "DOWN — run `fy host` on the Mac")
                )
        return rows


class CapabilityScope(Scope):
    """The external chain each active rung PROMISES (PAM grant, ADC, token validity) — the
    tier that used to fail silently. The action is the supervisor's probe loop
    (``run_capability_probes`` + atomic publish); here: render its published results. An
    unprobed axis with no probe registered makes no claim either way."""

    name = "capability"

    def rows(self, state: dict) -> list[ScopeRow]:
        mode = state["mode"]
        defaults = devmode.axis_defaults()
        if devmode.in_box():
            caps = state.get("capabilities") or {}
        else:
            caps = devmode.read_capabilities().get(config.active_worktree(), {})
        rows = []
        for axis, value in mode.items():
            if value == defaults.get(axis):
                continue
            cap = caps.get(axis)
            desired = f"{axis}={value} capability chain works"
            if cap is None:
                if any(p.axis == axis for p in devmode.capability_probes(mode)):
                    rows.append(ScopeRow("unknown", self.name, desired, "not probed yet"))
                continue  # no probe targets this axis — no claim either way
            if cap.get("ok"):
                rows.append(ScopeRow("ok", self.name, desired, cap.get("detail", "ok")))
            else:
                rows.append(
                    ScopeRow("drift", self.name, desired, f"DEGRADED — {cap.get('detail')}")
                )
        return rows or [ScopeRow("ok", self.name, "none (no active rung is probed)", "—")]


class StackScope(Scope):
    """The running compose stack vs the posture's overlay/profile signature. The action is
    the mode-change edge (:meth:`reconcile` → ``stack.reconcile_posture``); here: compose
    stamps every container with its ``-f`` list (the ``config_files`` label), so BOTH drift
    directions are observable — a desired overlay the stack lacks, and a stack still
    carrying a posture overlay the current mode no longer wants."""

    name = "stack"

    def reconcile(self, prev_posture: dict, new_posture: dict, cfg=None, sink=None) -> bool:
        """Bring an already-up stack to the new posture signature (the set_mode edge).
        Lazy import: stack imports devmode."""
        from . import stack

        return stack.reconcile_posture(prev_posture, new_posture, cfg=cfg, sink=sink)

    def rows(self, state: dict) -> list[ScopeRow]:
        signature = devmode.posture_signature(state["mode"])
        overlays = list(signature["overlays"])
        desired = f"overlays: {', '.join(o.rsplit('/', 1)[-1] for o in overlays) or 'none'}"
        profiles = signature["env"].get("COMPOSE_PROFILES", "")
        if profiles:
            desired += f" · profiles: {profiles}"
        try:
            out = subprocess.run(
                [
                    config.engine(),
                    "ps",
                    "--filter",
                    f"label=com.docker.compose.project={config.project_prefix()}",
                    "--format",
                    '{{.Label "com.docker.compose.project.config_files"}}',
                ],
                capture_output=True,
                text=True,
                timeout=5,
                env=devmode._engine_env(),
            )
            if out.returncode != 0:
                return [ScopeRow("unknown", self.name, desired, "engine unreachable")]
        except Exception:
            return [ScopeRow("unknown", self.name, desired, "engine unreachable")]
        lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
        if not lines:
            return [
                ScopeRow("ok", self.name, desired, "stack down (posture applies on next `fy up`)")
            ]
        config_files = max(lines, key=len)  # any container carries the full -f list
        # Filenames are the stable part (label paths may be absolute or relative; posture
        # overlays have distinct names by construction). Only files from the declared
        # [[overlay]] table count as posture overlays — base compose files are not ours.
        observed_names = {part.rsplit("/", 1)[-1] for part in config_files.split(",") if part}
        desired_names = {o.rsplit("/", 1)[-1] for o in overlays}
        known_names = {
            str(entry.get("file", "")).rsplit("/", 1)[-1]
            for entry in config.overlays_declared()
            if entry.get("file")
        }
        missing = sorted(desired_names - observed_names)
        stale = sorted((observed_names & known_names) - desired_names)
        if missing or stale:
            parts = []
            if missing:
                parts.append(f"WITHOUT overlay(s) {', '.join(missing)}")
            if stale:
                parts.append(f"still carrying stale overlay(s) {', '.join(stale)}")
            return [
                ScopeRow(
                    "drift",
                    self.name,
                    desired,
                    f"{len(lines)} containers {' and '.join(parts)} — "
                    "`fy up` re-renders (or change any mode to reconcile)",
                )
            ]
        return [
            ScopeRow("ok", self.name, desired, f"{len(lines)} containers on the posture overlays")
        ]


class BoxEnvScope(Scope):
    """The dev box's create-time baked env — the immutable-until-recreate tier. This scope
    CANNOT act (env can't change in a running box), so its whole job is the nag: reuse the
    dashboard's staleness hint. Host-only (the box can't inspect its own create args)."""

    name = "box"
    host_only = True

    def rows(self, state: dict) -> list[ScopeRow]:
        desired = "dev box env matches the posture"
        try:
            hint = devmode._box_env_hint(state["mode"])
        except Exception:
            return [ScopeRow("unknown", self.name, desired, "engine unreachable")]
        if hint:
            return [ScopeRow("drift", self.name, desired, hint.lstrip("⚠ "))]
        return [ScopeRow("ok", self.name, desired, "current (or box down)")]


def scopes() -> list[Scope]:
    """Every reconciled tier, in display order. THE inventory — a new tier registers here."""
    return [PostureScope(), DaemonScope(), CapabilityScope(), StackScope(), BoxEnvScope()]
