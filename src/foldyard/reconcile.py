"""The reconciler scope contract — every state tier in ONE inventory (proposal A).

Posture state lives on tiers with different refresh lifetimes, and the audited bug history
(docs/archive/mode-state-consolidation.md) is mostly "two tiers disagreeing about one fact". This
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

Stdlib only at import (loads on the `fy state` path); the stack scope reads overlays with PyYAML,
imported lazily — it rides in with the bundled podman-compose.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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

    name = "mode"

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
                        f"mirror stale ({shown}s) — is the host supervisor running? `fy host`",
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
            return [ScopeRow("ok", self.name, "none (the mode demands no daemon)", "—")]
        daemons = (state.get("daemons") or {}) if devmode.in_box() else devmode.daemon_status(mode)
        offline = all(v == devmode.axis_defaults().get(a) for a, v in mode.items())
        rows = []
        for name, spec in wanted.items():
            seen = daemons.get(name) or {}
            up = seen.get("up")
            desired = f"{name} listening on :{spec['port']}"
            if seen.get("blocked"):  # the supervisor's gate refused it — its reason IS the fix
                rows.append(ScopeRow("drift", self.name, desired, f"BLOCKED — {seen['blocked']}"))
            elif up:
                rows.append(ScopeRow("ok", self.name, desired, "up"))
            elif up is None:
                rows.append(
                    ScopeRow(
                        "unknown", self.name, desired, "no status — supervisor running? `fy host`"
                    )
                )
            elif offline:
                rows.append(
                    ScopeRow(
                        "unknown", self.name, desired, "not running (offline — `fy up` starts it)"
                    )
                )
            else:
                rows.append(
                    ScopeRow("drift", self.name, desired, "DOWN — `fy host restart` on the host")
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
                if any(p.switch == axis for p in devmode.capability_probes(mode)):
                    rows.append(ScopeRow("unknown", self.name, desired, "not probed yet"))
                continue  # no probe targets this axis — no claim either way
            if cap.get("ok"):
                rows.append(ScopeRow("ok", self.name, desired, cap.get("detail", "ok")))
            else:
                rows.append(
                    ScopeRow("drift", self.name, desired, f"DEGRADED — {cap.get('detail')}")
                )
        return rows or [ScopeRow("ok", self.name, "none (no active level is probed)", "—")]


class StackScope(Scope):
    """The running compose stack vs its rendered config. The action is the mode-change edge
    (:meth:`reconcile` → ``stack.reconcile_posture``). The verdict is the compose provider's own
    per-service config hash against a fresh render (:mod:`foldyard.confighash`) — exactly what
    ``up`` compares, so it sees env-only posture changes and never flags a service no change
    touched. The ``config_files`` label comparison (both directions: a desired overlay a
    container lacks, a posture overlay it still carries) then says WHY a stale container is
    stale — and stands in, reporting ``unknown`` rather than ✓, when the render is unavailable."""

    name = "stack"

    def reconcile(self, prev_posture: dict, new_posture: dict, cfg=None, sink=None) -> bool:
        """Bring an already-up stack to the new posture signature (the set_mode edge).
        Lazy import: stack imports devmode."""
        from . import stack

        return stack.reconcile_posture(prev_posture, new_posture, cfg=cfg, sink=sink)

    def _desired_hashes(self) -> tuple[dict[str, str] | None, str, str]:
        """``(hashes, why, label)``: the fresh render's hashes (spanning the running profiles) or
        None with why, and the label the ACTIVE provider records its hash under — decided
        together, so a docker-compose override on podman can't pair one's hash with the
        other's label."""
        from . import confighash, stack

        try:
            ctx = stack.resolve(no_machine=True)
            hashes, why = confighash.desired(ctx, stack._running_extra_profiles(ctx))
            return hashes, why, confighash.label(ctx)
        except (Exception, SystemExit) as e:  # resolve aborts a missing worktree with SystemExit
            return None, f"{type(e).__name__}: {e}", ""

    def rows(self, state: dict) -> list[ScopeRow]:
        signature = devmode.posture_signature(state["mode"])
        overlays = list(signature["overlays"])
        desired = f"overlays: {', '.join(o.rsplit('/', 1)[-1] for o in overlays) or 'none'}"
        profiles = signature["env"].get("COMPOSE_PROFILES", "")
        if profiles:
            desired += f" · profiles: {profiles}"
        rows = devmode.ps_labels(
            ["--filter", f"label=com.docker.compose.project={config.project_prefix()}"], timeout=5
        )
        if rows is None:
            return [ScopeRow("unknown", self.name, desired, "engine unreachable")]
        containers = [
            (labels.get("com.docker.compose.service", ""), labels)
            for _, labels in rows
            if labels.get("com.docker.compose.project.config_files", "").strip()
        ]
        if not containers:
            return [
                ScopeRow("ok", self.name, desired, "stack down (the mode applies on next `fy up`)")
            ]
        why = _overlay_reasons(overlays, containers)
        fix = "`fy up` recreates them (or change any mode to reconcile)"
        hashes, unavailable, label = self._desired_hashes()
        if hashes is None:
            if why:
                return [ScopeRow("drift", self.name, desired, f"{_explain(why)} — {fix}")]
            return [
                ScopeRow(
                    "unknown",
                    self.name,
                    desired,
                    f"{len(containers)} containers on the mode overlays, but their rendered "
                    f"config can't be checked ({unavailable})",
                )
            ]
        compared = [(svc, labels) for svc, labels in containers if svc in hashes]
        stale = {svc for svc, labels in compared if labels.get(label) != hashes[svc]}
        if stale:
            reasons = {svc: why.get(svc) or "its rendered config changed" for svc in stale}
            return [ScopeRow("drift", self.name, desired, f"{_explain(reasons)} — {fix}")]
        outside = sorted({svc or "?" for svc, _ in containers if svc not in hashes})
        observed = f"{len(compared)} containers match the rendered config"
        if outside:
            observed += f"; {len(outside)} not in it ({', '.join(outside)})"
        return [ScopeRow("ok", self.name, desired, observed)]


def _overlay_reasons(overlays: list[str], containers: list[tuple[str, dict]]) -> dict[str, str]:
    """Service → why its ``config_files`` label disagrees with the posture's overlays, for the
    services that do. Compose stamps a container's ``-f`` list when it CREATES it and recreates
    only the services an overlay changes, so a label is only stale for the overlays that define
    that container's service (no single container "carries the full list"). Only the declared
    ``[[overlay]]`` table counts as posture overlays — base compose files are not ours; names are
    compared (label paths may be absolute or relative; overlay names are distinct)."""
    desired_names = {o.rsplit("/", 1)[-1] for o in overlays}
    declared: dict[str, Path] = {}
    for entry in config.overlays_declared():
        if path := config._overlay_path(entry.get("file")):
            declared[path.name] = path
    touches = _overlay_services({**declared, **{Path(o).name: Path(o) for o in overlays}})
    missing: dict[str, set[str]] = {}
    extra: dict[str, set[str]] = {}
    for service, labels in containers:
        files = labels.get("com.docker.compose.project.config_files", "")
        observed_names = {part.rsplit("/", 1)[-1] for part in files.split(",") if part}
        for name in desired_names - observed_names:
            if _touches(touches, name, service):
                missing.setdefault(service or "?", set()).add(name)
        for name in (observed_names & declared.keys()) - desired_names:
            if _touches(touches, name, service):
                extra.setdefault(service or "?", set()).add(name)
    out: dict[str, str] = {}
    for service in sorted(missing.keys() | extra.keys()):
        parts = []
        if service in missing:
            parts.append(f"WITHOUT overlay {', '.join(sorted(missing[service]))}")
        if service in extra:
            parts.append(f"still carrying stale overlay {', '.join(sorted(extra[service]))}")
        out[service] = " and ".join(parts)
    return out


def _explain(reasons: dict[str, str]) -> str:
    """``{service: reason}`` as one line, services sharing a reason grouped."""
    grouped: dict[str, list[str]] = {}
    for service, reason in sorted(reasons.items()):
        grouped.setdefault(reason, []).append(service)
    return "; ".join(f"{', '.join(services)}: {reason}" for reason, services in grouped.items())


def _overlay_services(paths: dict[str, Path]) -> dict[str, set[str] | None]:
    """Overlay filename → the compose services it defines; ``None`` when that can't be read
    (missing, unparseable, not a mapping) — which :func:`_touches` reads as "every service",
    so an unanswerable question reports drift rather than a silent ✓. PyYAML rides in with the
    bundled podman-compose and is imported only here, off the hot path (as ``stack`` does)."""
    import yaml

    out: dict[str, set[str] | None] = {}
    for name, path in paths.items():
        try:
            services = (yaml.safe_load(path.read_text()) or {}).get("services") or {}
            out[name] = set(services) if isinstance(services, dict) else None
        except (OSError, yaml.YAMLError, AttributeError):
            out[name] = None
    return out


def _touches(touches: dict[str, set[str] | None], overlay: str, service: str) -> bool:
    """Whether ``overlay`` shapes ``service`` — conservatively True when either is unknown."""
    services = touches.get(overlay)
    return services is None or not service or service in services


class BoxEnvScope(Scope):
    """The dev box's create-time baked env — the immutable-until-recreate tier. This scope
    CANNOT act (env can't change in a running box), so its whole job is the nag: reuse the
    dashboard's staleness hint. Host-only (the box can't inspect its own create args)."""

    name = "box"
    host_only = True

    def rows(self, state: dict) -> list[ScopeRow]:
        desired = "dev box env matches the mode"
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
