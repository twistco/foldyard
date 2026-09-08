#!/usr/bin/env python3
"""Dev-posture mode state — the substrate behind `fy mode`, `fy host` and the TUI.

The stack has two orthogonal credential axes, each a ladder from "zero secrets" to
"emergency, my own identity":

  gcp:     off | logs | sa | user        github:  off | app | user
    off    emulators only — zero secrets   off    no GitHub credential anywhere
    logs   dev box: read-only Cloud        app    PR/issue comments on the one repo
           Logging (the box log-reader SA)        (App installation token, injected
    sa     + apps hit real staging GCP            host-side by the egress proxy)
           (the app runtime SA)            user   EMERGENCY: your own `gh` token
    user   EMERGENCY: your own GCP                injected (push becomes possible)
           identity inside the box

"Mode" is a desired posture, not a capability: the Mac-side daemons (`fy host`) are
the enforcement point — without them running, a mode grants nothing. Which is why the
state lives in TWO files:

  ~/.foldyard/<project>/dev-mode.json
                                 AUTHORITATIVE. Mac home, NOT the shared repo mount, so
                                 nothing inside the VM/box can escalate its own posture
                                 by editing it. Only `fy mode …` on the Mac writes it.
  <repo>/<dev_vm_dir>/.dev-mode.json
                                 READ-ONLY MIRROR (gitignored), written by the Mac on
                                 every mode change + supervisor heartbeat so box
                                 sessions can SEE the posture (`fy mode`). Purely
                                 informational — no daemon ever grants based on it.

`user` modes are structurally ephemeral: they carry a TTL (default 1h, max 8h), every
reader treats a lapsed TTL as `off`, and the supervisor kills the daemon + writes the
axis back to `off` at expiry. Emergencies that need re-arming never become the default.

Stdlib only (runs on the Mac system python3 and in the box). The TUI (tui.py) and the
supervisor (supervisor.py) import this module; the `foldyard` CLI (cli.py) routes to
its main():

  foldyard mode                    the dashboard (Mac: live probes; box: the mirror)
  foldyard mode gcp=logs [ttl=1h]  set axes (Mac only); ttl applies to user modes
  foldyard mode env                shell `export` lines deriving recipe env from the
                                   mode (only as defaults — explicit env always wins)
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config, configpin
from .machine_backend import get_backend
from .plugins import DoctorContext, registry

# Axes / rungs / blurbs / daemons / env / doctor checks are PLUGIN-defined (ADR-0015), and the
# ACTIVE set is now a function of the RESOLVED CONFIG (per-consumer-registry-plan.md Steps A/B):
# a different consumer/worktree can have a different axis set, and a long-lived `fy host`/TUI must
# see config changes. So the old import-time snapshot (AXES/MODE_BLURB/… frozen for the process
# lifetime) is gone — these are thin ACCESSORS that read the live registry on demand. The host
# state paths (mode file, mirror, host.env) are likewise read live via `config` rather than
# snapshotted, so they too track the active config/worktree. Everything below is the
# project-agnostic SPINE — it never names a specific credential mechanism.
DEFAULT_TTL = 3600
MAX_TTL = 8 * 3600

PODMAN_MACHINE = config.machine_name()
_BACKEND = get_backend(config.machine_backend())


def axes() -> dict[str, tuple[str, ...]]:
    """axis -> its rungs (rung 0 = the zero-secret default), from the active registry."""
    return registry().axis_rungs()


def axis_defaults() -> dict[str, str]:
    """axis -> its zero-secret resting rung (rung 0; usually but not necessarily "off" — e.g.
    storage's "local", auth0's "sim"), from the active registry. Unset/invalid/expired values
    read as this."""
    return registry().axis_defaults()


def mode_blurb() -> dict[tuple[str, str], str]:
    """(axis, rung) -> one-line human description, from the active registry."""
    return registry().blurbs()


def axis_daemon() -> dict[str, str | None]:
    """axis -> the daemon name its status maps to (None ⇒ no daemon), from the active registry."""
    return registry().axis_daemon()


def emergency() -> dict[str, tuple[str, ...]]:
    """axis -> rungs that carry a TTL + auto-revert, from the active registry."""
    return registry().emergency_rungs()


def in_box() -> bool:
    """The dev-box signature — delegates to config.in_box (the single source of truth;
    tests still monkeypatch devmode.in_box, which this and devmode's own callers use)."""
    return config.in_box()


def clock_offset() -> float:
    """TESTING clock skew (seconds) added to :func:`now` — ``fy clock ff 2h`` writes it, so TTL
    expiry / auto-revert / the settle cascade can be exercised live without waiting (see
    docs/testing-modes.md). ``FOLDYARD_CLOCK_OFFSET`` env wins (one-shot runs); else the
    host-side ``clock-offset`` state file, which a long-lived supervisor re-reads every ``now()``
    so a fast-forward lands within a tick. Missing/invalid = 0 — the normal, unskewed state."""
    env = os.environ.get("FOLDYARD_CLOCK_OFFSET")
    if env:
        try:
            return _sane_offset(float(env))
        except ValueError:
            return 0.0
    try:
        return _sane_offset(float(config.clock_offset_file().read_text().strip()))
    except (OSError, ValueError):
        return 0.0


# The skew ceiling (±10 years): far beyond any TTL test, small enough that the timedelta
# arithmetic in now() can never overflow datetime's range.
_MAX_CLOCK_OFFSET = 10 * 365 * 24 * 3600.0


def _sane_offset(offset: float) -> float:
    """0.0 for a non-finite or absurd parsed skew — a corrupt offset file (or an env typo like
    ``inf``) must read as "no skew", never crash the supervisor's now() arithmetic."""
    if not math.isfinite(offset) or abs(offset) > _MAX_CLOCK_OFFSET:
        return 0.0
    return offset


def now() -> datetime:
    offset = clock_offset()
    current = datetime.now(UTC)
    return current + timedelta(seconds=offset) if offset else current


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _parse(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None


def parse_ttl(text: str) -> int:
    """'90s' / '30m' / '2h' / plain seconds → seconds, clamped to MAX_TTL."""
    text = text.strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600}.get(text[-1:], None)
    seconds = int(text[:-1]) * mult if mult else int(text)
    if seconds <= 0:
        raise ValueError(f"ttl must be positive: {text!r}")
    return min(seconds, MAX_TTL)


# ── state files ────────────────────────────────────────────────────────────────────


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def read(apply_expiry: bool = True) -> dict:
    """Current posture from the right source for where we run.

    Returns {"mode": {axis: value}, "expires": {axis: iso}, "written": iso|None,
    "daemons": {...}|None, "source": str}. With apply_expiry, a lapsed TTL reads as
    off (the supervisor separately writes that back).
    """
    src = config.mirror_file() if in_box() else config.mode_file()
    raw = _load(src)
    rungs = axes()
    defaults = axis_defaults()
    # Unknown axes in the file (e.g. a renamed axis's stale key) are simply ignored; unset or
    # invalid values read as the axis's own default rung (rung 0 — not always "off").
    mode = {axis: raw.get(axis, defaults[axis]) for axis in rungs}
    for axis, value in mode.items():
        if value not in rungs[axis]:
            mode[axis] = defaults[axis]
    expires = {a: t for a, t in (raw.get("expires") or {}).items() if a in rungs}
    if apply_expiry:
        for axis, iso in list(expires.items()):
            exp = _parse(iso)
            if exp is None or exp <= now():
                mode[axis] = defaults[axis]
                del expires[axis]
    return {
        "mode": mode,
        "expires": expires,
        "written": raw.get("written"),
        "daemons": raw.get("daemons"),
        "capabilities": raw.get("capabilities"),  # box mirror only; the host reads the state file
        "source": str(src),
    }


def write_mirror(
    mode: dict, expires: dict, daemons: dict | None, capabilities: dict | None = None
) -> None:
    """The informational copy on the shared mount, for box sessions. Mac only. ``capabilities``
    is the supervisor's probe results for THIS worktree (axis → {ok, detail, checked}) so an
    in-box ``fy mode`` can render a DEGRADED axis; callers without probe results (``fy mode``
    itself) pass None and the mirror simply carries no capability claim until the next tick."""
    payload = {**mode, "expires": expires, "written": _iso(now()), "daemons": daemons or {}}
    if capabilities is not None:
        payload["capabilities"] = capabilities
    config.mirror_file().write_text(json.dumps(payload, indent=2) + "\n")


def set_mode(
    updates: dict[str, str],
    ttl: int | None = None,
    force: bool = False,
    reconcile: bool = True,
    reconcile_sink: Callable[[str], None] | None = None,
) -> dict:
    """Apply axis updates to the authoritative file (+ mirror). Mac only.

    ``force`` downgrades coherence ERRORS to printed warnings and applies anyway — for callers
    that must never be refused (the supervisor's TTL expiry is a de-escalation; the dependent
    axis it strands is surfaced by ``fy mode`` and blocks the NEXT interactive set instead).

    ``reconcile_sink`` routes the stack-profile reconcile's output (the TUI passes a log-pane sink
    so compose output doesn't bleed over the Textual UI; default None → stderr)."""
    if in_box():
        raise SystemExit(
            "✗ mode changes are Mac-only: the box must not escalate its own posture "
            "(the authoritative file lives in the Mac home, outside the shared mount)."
        )
    rungs = axes()
    emergency_rungs = emergency()
    for axis, value in updates.items():
        if axis not in rungs:
            raise SystemExit(f"✗ unknown axis {axis!r} (have: {', '.join(rungs)})")
        if value not in rungs[axis]:
            raise SystemExit(f"✗ {axis} mode {value!r} (have: {', '.join(rungs[axis])})")

    state = read(apply_expiry=True)
    mode, expires = state["mode"], state["expires"]
    prev_posture = posture_signature(mode)  # before applying the updates
    for axis, value in updates.items():
        mode[axis] = value
        if value in emergency_rungs.get(axis, ()):  # plugin-declared emergency rungs carry a TTL
            expires[axis] = _iso(now() + timedelta(seconds=ttl or DEFAULT_TTL))
        else:
            expires.pop(axis, None)

    # Coherence gate (plugin mode_issues, on the FULL prospective mode): an "error" combination
    # cannot function (e.g. llm=record without the identity it consumes), so refuse it — the
    # message carries the complete `fy mode a=x b=y` fix, and updates apply atomically, so a
    # de-escalation is never trapped (include the dependent axes in one command). "warn" rows
    # print (stderr) but the mode still applies.
    issues = registry().mode_issues(mode)
    errors = [msg for sev, msg in issues if sev == "error"]
    if errors and not force:
        raise SystemExit("✗ refusing an incoherent mode:\n  " + "\n  ".join(errors))
    for msg in errors:  # force: applied anyway, but say what's broken
        print(f"⚠ {msg}", file=sys.stderr)
    for sev, msg in issues:
        if sev == "warn":
            print(f"⚠ {msg}", file=sys.stderr)

    auth_file = config.mode_file()
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text(json.dumps({**mode, "expires": expires}, indent=2) + "\n")
    # Refresh an existing mirror, but CREATE one only while this checkout's box is up: `fy mode`
    # (and the supervisor's TTL expiry, which lands here) against a fully-down project must not
    # re-dirty a clean checkout — the supervisor seeds the mirror within a tick of a box-up.
    if config.mirror_file().exists() or config.active_worktree() in up_worktrees():
        write_mirror(mode, expires, daemon_status(mode))
    new_posture = posture_signature(mode)
    # set_mode is the single Mac-side choke point every posture change routes through (CLI + TUI +
    # the supervisor's TTL expiry), so it's where the running stack is reconciled to the new
    # posture — whenever the posture SIGNATURE (derived env incl. COMPOSE_PROFILES, plus the
    # overlay -f list) changed: profile toggles (gcp=off→logs adds the metadata emulator) AND
    # overlay/env-only changes (llm=off→record flips TANGIBLE_LLM_MODE) both count. Best-effort +
    # already-up-only; see stack.reconcile_posture. `reconcile=False` lets the TUI apply the file
    # FIRST (instant button highlight) then run the slow compose reconcile off the UI thread via
    # `reconcile_stack`.
    if reconcile:
        reconcile_stack(prev_posture, new_posture, sink=reconcile_sink)
    return {
        "mode": mode,
        "expires": expires,
        "prev_posture": prev_posture,
        "new_posture": new_posture,
        # kept for display convenience (the TUI's notify line)
        "prev_profiles": prev_posture["env"].get("COMPOSE_PROFILES", ""),
        "new_profiles": new_posture["env"].get("COMPOSE_PROFILES", ""),
    }


def posture_signature(mode: dict) -> dict:
    """Everything about a posture that shapes the RUNNING stack's compose config: the derived
    env (COMPOSE_PROFILES + the mode_env compose interpolates into service definitions) and the
    overlay ``-f`` list. The reconcile compares two of these to decide whether an already-up
    stack must be re-upped — comparing COMPOSE_PROFILES alone missed overlay/env-only posture
    changes (e.g. llm=off→record adds compose.llm.yml with no profile change)."""
    return {"env": derive_env(mode), "overlays": registry().compose_overlays(mode)}


def reconcile_stack(
    prev_posture: dict, new_posture: dict, sink: Callable[[str], None] | None = None
) -> bool:
    """Reconcile the running stack to a posture signature (see ``posture_signature``). Split out
    of set_mode so the TUI can apply the mode file synchronously (instant feedback) and run this
    — the slow compose subprocess — in a worker thread with a log-pane sink. Returns False when
    the compose run itself failed (the TUI surfaces that); True otherwise (incl. no-op paths).
    Routed through the stack SCOPE (reconcile.StackScope — the scope inventory), which
    delegates to stack.reconcile_posture. Lazy import: reconcile imports devmode."""
    from . import reconcile as reconcile_mod

    return reconcile_mod.StackScope().reconcile(
        prev_posture, new_posture, cfg=config.current(), sink=sink
    )


def settle_incoherent(mode: dict) -> dict[str, str]:
    """Extra axis→default flips that make ``mode`` coherent — the expiry cascade (proposal E).

    TTL expiry is a forced de-escalation that must never be refused, but reverting one axis can
    strand a dependent one on a ``mode_issues`` *error* combination (a rung that can only fail
    without the identity it consumes). Rather than leave the stranded axis erroring until the
    next interactive ``fy mode``, settle: greedily flip non-default axes to their default while
    each flip strictly reduces the error count. Downgrades only — settling never raises a rung —
    so the worst case is "everything at rest", and an emergency lapse always lands on a posture
    that WORKS offline. Plugin-agnostic on purpose: it needs no structured dependency
    declaration, just the existing ``mode_issues`` gate, so any future axis participates for
    free. Returns only the extra flips (not the whole mode); {} when already coherent or when no
    single downgrade helps (then ``fy mode`` keeps surfacing the error, as before)."""

    def _errors(m: dict) -> list[str]:
        return [msg for sev, msg in registry().mode_issues(m) if sev == "error"]

    flips: dict[str, str] = {}
    current = dict(mode)
    defaults = axis_defaults()
    while errors := _errors(current):
        for axis, default in defaults.items():
            if current.get(axis, default) == default:
                continue
            trial = {**current, axis: default}
            if len(_errors(trial)) < len(errors):
                current = trial
                flips[axis] = default
                break
        else:
            # No SINGLE downgrade reduces the errors — a combination may only clear together
            # (two dependent axes whose error mentions both). Coordinated fallback: try
            # everything-at-rest; if THAT is coherent, settle every non-default axis down.
            # Still downgrade-only; if even all-defaults errors (a plugin's unconditional
            # error), leave the posture visible rather than thrash.
            all_default = dict(defaults)
            if not _errors(all_default):
                flips.update(
                    {a: d for a, d in defaults.items() if current.get(a, d) != d},
                )
            break
    return flips


# ── daemons (what `fy host` must run for a mode) ─────────────────────────────────


def desired_daemons(mode: dict) -> dict[str, dict]:
    """name → spec for every host daemon this mode demands (merged across plugins)."""
    return registry().desired_daemons(mode)


def capability_probes(mode: dict) -> list:
    """The plugin capability probes this mode activates (merged across plugins) — run by the
    supervisor each tick (due ones only); see plugins.CapabilityProbe."""
    return registry().capability_probes(mode)


def read_capabilities() -> dict:
    """The supervisor's published probe results, worktree-keyed (``""`` = main):
    {wt: {axis: {ok, detail, checked}}}. Host side reads the project state file; in the box the
    per-worktree mirror carries this worktree's slice (see ``read()``), so callers there should
    prefer ``read()["capabilities"]``. Missing/unreadable = {} (no claim either way)."""
    return _load(config.capabilities_file())


def degraded_capabilities(mode: dict | None = None) -> list[tuple[str, str]]:
    """``(axis, detail)`` for every ACTIVE (non-default) axis whose probed capability is
    failing — the claim ``fy mode`` renders as ⚠ DEGRADED, packaged for verbs like ``fy up``
    that should not finish silently under a lapsed credential chain. Reads this worktree's
    slice from whichever tier is local (host: the state file; box: the mirror). Best-effort:
    a missing file/entry is "no claim", never a warning."""
    state = read()
    if mode is None:
        mode = dict(state["mode"])
    if in_box():
        capabilities = state.get("capabilities") or {}
    else:
        capabilities = read_capabilities().get(config.active_worktree(), {})
    defaults = axis_defaults()
    out: list[tuple[str, str]] = []
    for axis, value in mode.items():
        cap = capabilities.get(axis)
        if value != defaults.get(axis) and isinstance(cap, dict) and not cap.get("ok"):
            out.append((axis, str(cap.get("detail") or "capability probe failing")))
    return out


def probe(port: int, host: str | None = None) -> bool:
    # In-box, Mac daemon ports are probed at the backend's guest→host address (config.host_alias:
    # host.containers.internal under podman-machine/native, the Lima host gateway IP under lima).
    host = host or (config.host_alias() if in_box() else "127.0.0.1")
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def daemon_status(mode: dict) -> dict[str, dict]:
    return {
        name: {"label": spec["label"], "port": spec["port"], "up": probe(spec["port"])}
        for name, spec in desired_daemons(mode).items()
    }


# ── recipe env derivation ──────────────────────────────────────────────────────────


def derive_env(mode: dict) -> dict[str, str]:
    """Recipe defaults the current mode implies (merged across plugins). Emitted as
    `export K="${K:-v}"` so an explicitly exported variable always wins over the mode file."""
    return registry().derive_env(mode)


def env_defaults(mode: dict) -> dict[str, str]:
    """HOST-process env defaults this mode's plugins can derive from committed config (merged
    across plugins) — see ``plugins.Plugin.env_defaults``. The supervisor applies these via
    ``os.environ.setdefault`` each tick, before daemon `requires` gates and launch env are read."""
    return registry().env_defaults(mode)


# ── workspaces (main + worktrees) — feeds the TUI's left column ────────────────────


def _engine_env() -> dict[str, str]:
    """Ambient env + the machine backend's engine socket (``DOCKER_HOST``/``CONTAINER_HOST``), for
    the read-only engine probes in this module (:func:`workspaces`, :func:`up_worktrees`,
    :func:`_box_env_hint`) and ``reconcile``'s scopes. ``stack.resolve()`` exports the socket for
    the real verbs; these probes shell the engine directly, and bare they only worked on the
    podman backend BY ACCIDENT — ``podman machine init`` registers itself as podman's default
    connection, lima does not, so a bare ``podman ps`` missed a running lima box and the TUI
    said "no devbox" while `fy box shell` (which resolves) attached fine. A preset DOCKER_HOST
    (in-box, or an operator override) wins; a backend that can't name a socket yet (no machine)
    degrades to the ambient env — the probe then fails exactly as before ("engine unreachable").
    Never ``machine.ensure()`` here: a 5s TUI refresh must not provision a VM."""
    env = dict(os.environ)
    if env.get("DOCKER_HOST"):
        return env
    try:
        from . import machine  # lazy — keep devmode importable without the VM tier

        sock = machine.socket()
    except Exception:
        return env
    env["DOCKER_HOST"] = sock
    env["CONTAINER_HOST"] = sock
    return env


def workspaces() -> list[dict]:
    """main + each worktree, with stack/devbox status from one engine call.

    Each entry: {name, path, project, branch, app_port, containers (compose count, None
    if the engine is unreachable), devbox (bool)}.
    """
    repo = config.repo_root()
    wt_root = config.worktrees_root(repo)
    prefix = config.project_prefix()
    items: list[dict[str, Any]] = [{"name": "main", "path": str(repo), "project": prefix}]
    if wt_root.is_dir():
        for d in sorted(wt_root.iterdir()):
            if d.is_dir() and (d / ".git").exists():
                items.append({"name": d.name, "path": str(d), "project": f"{prefix}-{d.name}"})
    try:
        out = subprocess.run(
            [
                config.engine(),
                "ps",
                "--format",
                '{{.Names}}\t{{.Label "com.docker.compose.project"}}',
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env=_engine_env(),
        )
        rows = [line.split("\t") for line in out.stdout.splitlines() if "\t" in line]
        if out.returncode != 0:
            rows = None
    except Exception:
        rows = None
    for item in items:
        wt = "" if item["name"] == "main" else item["name"]
        try:
            with config.using(worktree_config(wt)):
                key = config.app_port_key()
                base = config.port_bases().get(key) if key else None
                item["app_port"] = base + config.worktree_offset(wt) if base is not None else None
        except Exception:
            # A worktree can temporarily contain an invalid/older foldyard.toml while switching
            # branches. Keep the card usable; the missing port makes the browser action explain
            # the config error when invoked rather than taking down the whole TUI refresh.
            item["app_port"] = None
        try:
            item["branch"] = subprocess.run(
                ["git", "-C", item["path"], "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
        except Exception:
            item["branch"] = ""
        if rows is None:
            item["containers"] = None
            item["devbox"] = False
            continue
        item["containers"] = sum(1 for _, proj in rows if proj == item["project"])
        item["devbox"] = any(name == f"{item['project']}-devbox" for name, _ in rows)
    return items


def main_repo() -> Path:
    if os.environ.get("FOLDYARD_REPO"):
        return config.repo_root()
    try:
        from . import stack

        return stack.main_repo()
    except Exception:
        return config.repo_root()


# ── per-worktree reconcile set (the ONE supervisor serves N worktrees) ──────────────────


def worktree_keys() -> list[str]:
    """Every checkout that EXISTS, as worktree keys (``""`` = the main checkout, then each worktree
    dir). The candidate set the supervisor + ``fy mode`` iterate; :func:`active_worktrees` narrows
    to the ones whose box is up."""
    keys = [""]
    # Anchor on the PRIMARY checkout (main_repo), not config.repo_root(): a worktree-relative caller
    # (or a bound worktree config) makes repo_root() the active worktree, so worktrees_root() would
    # resolve to a bogus `<worktree>-worktrees` instead of the real sibling-worktrees dir.
    wt_root = config.worktrees_root(main_repo())
    if wt_root.is_dir():
        for d in sorted(wt_root.iterdir()):
            if d.is_dir() and (d / ".git").exists():
                keys.append(d.name)
    return keys


def up_worktrees() -> list[str]:
    """Worktree keys (``""`` = main) whose dev box is ACTUALLY up right now — [] when none is
    (or the engine is unreachable: a stopped/deleted machine has no up boxes by definition).
    One engine ``ps``. This is the set whose repo mirrors the supervisor may write; see
    :func:`active_worktrees` for the daemon-serving set, which keeps a main fallback."""
    prefix = config.project_prefix()
    # Mirror stack._context's PODMAN_PROJECT sanitization: worktree names can contain slashes
    # (e.g. a branch-derived name), which podman project/container names can't — keep this in
    # sync with the same replace() there so the two never compute different project names for
    # the same worktree.
    proj = {
        wt: (prefix if not wt else f"{prefix}-{wt.replace('/', '-')}") for wt in worktree_keys()
    }
    try:
        out = subprocess.run(
            [config.engine(), "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=5,
            env=_engine_env(),
        )
        if out.returncode != 0:
            return []
        names = set(out.stdout.split())
    except Exception:
        return []
    return [wt for wt, p in proj.items() if f"{p}-devbox" in names]


def active_worktrees() -> list[str]:
    """The set the one supervisor reconciles DAEMONS for: every worktree whose dev box is up
    (a posture change must never START a heavy stack, so only up boxes count —
    ADR-0016 guardrail), falling back to ``[""]`` (main) when none is or the
    engine is unreachable, so a box coming up always finds a live proxy + its CA. The fallback
    is daemon-only: mirror writes key on :func:`up_worktrees`, so an idle supervisor never
    keeps re-dropping ``.dev-mode.json`` into a checkout nothing is reading."""
    return up_worktrees() or [""]


def worktree_config(wt: str) -> config.Config:
    """A resolved :class:`~foldyard.config.Config` for worktree ``wt`` (``""`` = main): repo_root
    is that checkout (so its mirror + ``dev_vm_dir`` resolve there) and ``worktree=wt`` (so its
    posture file, logs, and daemon ports all key on it). Bind it (``with config.using(…)``) to
    read/reconcile that worktree's posture.

    The TOML is that checkout's **adopted** copy, not its working tree (:func:`configpin.effective`)
    — this is the funnel every host-side consumer of a worktree's config goes through (the
    supervisor's reconcile loop, the TUI, ``fy state``), so pinning it here is what keeps a repo
    edit from reaching the Mac's credential daemons unattended. In the box, and before anything has
    been adopted, it falls back to the working tree exactly as before."""
    if not wt:
        bound = config.bound_config()
        if bound is not None and bound.worktree == "" and not os.environ.get("WORKTREE"):
            return bound
        return configpin.effective(config.resolve(worktree="", repo=main_repo()))
    # Same primary-anchor as worktree_keys(): the sibling path must key off main_repo, not the
    # (possibly worktree) active checkout, or a bound-config caller resolves the wrong dir.
    return configpin.effective(
        config.resolve(worktree=wt, repo=config.worktrees_root(main_repo()) / wt)
    )


def branches() -> list[str]:
    """Existing branch names (local + remote, origin/ stripped, deduped) for the
    new-worktree picker. Local first, then remote-only ones, each group sorted."""
    repo = main_repo()
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads",
                "refs/remotes",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    local, remote = [], []
    for ref in out.stdout.split():
        if ref.startswith("origin/"):
            name = ref[len("origin/") :]
            if name != "HEAD":
                remote.append(name)
        else:
            local.append(ref)
    seen, ordered = set(), []
    for name in sorted(local) + sorted(remote):
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _fy(args: list[str], timeout: float = 60, env_extra: dict | None = None) -> tuple[int, str]:
    """Run a `foldyard` (≡ `fy`) verb from the repo root (Mac only). Returns (rc, combined
    out). `env_extra` overlays the process env (e.g. WORKTREE=<name> to target a worktree)."""
    try:
        env = {**os.environ, **(env_extra or {})}
        out = subprocess.run(
            ["foldyard", *args],
            cwd=str(main_repo()),
            # stdin CLOSED. The TUI calls these while Textual owns the terminal in raw mode, and
            # an inherited stdin is still a TTY — so a verb that gates on `sys.stdin.isatty()`
            # (configpin.gate, on every launch verb) decides it may prompt, then blocks on an
            # `input()` nobody can see or answer, wedging `fy box up` from the TUI. Closed, the
            # gate correctly reads "no terminal here" and returns its actionable error instead.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return out.returncode, _strip_ansi((out.stdout + out.stderr).strip())
    except Exception as e:
        return 127, str(e)


def create_worktree(name: str, branch: str) -> tuple[int, str]:
    """Shell out to `fy worktree add` (Mac only — it touches git + the machine).
    Returns (returncode, combined output). `name` is the worktree dir name."""
    if not name:
        return 1, "empty worktree name"
    return _fy(["worktree", "add", name, *([branch] if branch else [])], timeout=180)


def remove_worktree(name: str) -> tuple[int, str]:
    """Shell out to `fy worktree remove <name> --yes` (Mac only): tear down the worktree's dev box
    + stack (compose down + drop its volumes), archive its Claude transcripts, then git-remove the
    checkout. `--yes` skips the CLI's own prompt — the TUI shows its own confirmation first. The
    box+stack teardown is the slow part, so allow a generous timeout. Returns (rc, combined out)."""
    if not name or name == "main":
        return 1, "refusing to remove the main checkout"
    return _fy(["worktree", "remove", name, "--yes"], timeout=300)


def open_code(name: str) -> tuple[int, str]:
    """Open VS Code on a workspace via `fy code` (Mac only). `main` runs it at the repo
    root; a worktree targets it with `WORKTREE=<name> fy code`."""
    env_extra = {"WORKTREE": "" if name == "main" else name}
    return _fy(["code"], timeout=60, env_extra=env_extra)


def open_browser(name: str) -> tuple[int, str]:
    """Open a workspace's localhost app via ``fy open`` (host only)."""
    env_extra = {"WORKTREE": "" if name == "main" else name}
    return _fy(["open"], timeout=60, env_extra=env_extra)


def box_up(name: str) -> tuple[int, str]:
    """Create + start a workspace's dev box via `fy box up` (Mac only). The first run on a
    fresh image builds + warms deps, so allow a generous timeout."""
    env_extra = {"WORKTREE": "" if name == "main" else name}
    return _fy(["box", "up"], timeout=600, env_extra=env_extra)


def box_down(name: str) -> tuple[int, str]:
    """Stop + remove a workspace's dev box via `fy box down` (Mac only; login/CLI volumes
    are kept — that's `fy box down`'s own contract)."""
    env_extra = {"WORKTREE": "" if name == "main" else name}
    return _fy(["box", "down"], timeout=120, env_extra=env_extra)


def machine_state() -> str:
    """The rootless machine's state: 'running' | 'stopped' | 'unknown' (via the active
    backend — podman or Lima)."""
    return _BACKEND.state(PODMAN_MACHINE) or "unknown"


def machine_toggle() -> tuple[int, str]:
    """Start the machine if stopped, stop it if running (Mac only). Returns (rc, combined
    output). No-op with a clear message if the state is unknown. Uses the active backend's
    start/stop commands so the TUI still surfaces their raw output."""
    state = machine_state()
    if state == "running":
        return _run(_BACKEND.stop_argv(PODMAN_MACHINE), timeout=60)
    if state in ("stopped", "unknown"):
        return _run(_BACKEND.start_argv(PODMAN_MACHINE), timeout=180)
    return 1, f"machine state {state!r} — not toggling (run {_BACKEND.cli} machine ls)"


# ── doctor — "what can I even grant from here?" setup checks ───────────────────────


def _which(cmd: str) -> bool:
    from shutil import which

    return which(cmd) is not None


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    """Drop terminal control sequences. gcloud (and friends) colour their output even
    when not on a tty; the raw ESC[…m bytes corrupt any downstream markup/parsing."""
    return _ANSI_RE.sub("", text)


# Ring buffer of subprocess calls (command + rc + captured output) so the TUI's Doctor
# tab can show what ran in the background. Reset per doctor run via cmd_log_reset().
_CMD_LOG: list[dict] = []
_CMD_LOG_MAX = 200


def cmd_log_reset() -> None:
    _CMD_LOG.clear()


def cmd_log() -> list[dict]:
    return list(_CMD_LOG)


# Commands whose SUCCESSFUL output is a credential (token / PEM). We still log the
# command and its rc, but never the secret it printed. (On failure the output is an
# error message, not the secret, so it's kept — it's what you need to debug.)
_SECRET_OUTPUT = (
    "print-access-token",
    "print-identity-token",
    "auth token",
    "secrets versions access",
)


def _redact_for_log(cmd_str: str, rc: int, output: str) -> str:
    if rc == 0 and output and any(s in cmd_str for s in _SECRET_OUTPUT):
        return "<output redacted — credential>"
    return output


def _run(cmd: list[str], timeout: float = 8) -> tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        rc, output = out.returncode, _strip_ansi((out.stdout or out.stderr).strip())
    except Exception as e:
        rc, output = 127, str(e)
    cmd_str = " ".join(cmd)
    _CMD_LOG.append({"cmd": cmd_str, "rc": rc, "out": _redact_for_log(cmd_str, rc, output)})
    del _CMD_LOG[:-_CMD_LOG_MAX]
    return rc, output  # callers still get the real output; only the log is redacted


def _result(ok: bool | None, name: str, good: str, bad: str) -> tuple[str, str, str]:
    return ("ok" if ok else "warn" if ok is None else "fail", name, good if ok else bad)


def run_stream(
    cmd: list[str],
    on_line: Callable[[str], None],
    timeout: float = 600.0,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> int:
    """Run ``cmd`` to completion, calling ``on_line(text)`` for each output line (stdout+stderr
    merged, ANSI-stripped), and return its exit code. For long-running, NON-INTERACTIVE
    subprocesses whose progress should stream live — the TUI's doctor fixes and the posture
    reconcile's compose — unlike ``_run`` (which buffers and has a short timeout). A watchdog
    kills the process after ``timeout`` seconds so a wedged command can't hang the worker
    forever. Deliberately NOT logged to the command log: the live stream is the record, and
    double-logging would duplicate it under the fix output in the Doctor pane."""
    import threading

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=cwd,
        )
    except Exception as e:
        on_line(f"✗ {e}")
        return 127
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    rc = 127
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            on_line(_strip_ansi(line.rstrip("\n")))
        rc = proc.wait()
    finally:
        timer.cancel()
    if rc < 0:
        on_line(f"✗ process killed after {int(timeout)}s (timeout)")
    return rc


def host_command() -> list[str]:
    """The command that launches the host supervisor — ``foldyard host``, the SAME process
    ``fy host`` runs. Prefer the ``foldyard`` console script beside this interpreter (the uv
    tool venv) so the TUI launches its own install, falling back to whatever ``foldyard`` is on
    PATH. The TUI runs this as a child it can stream + stop (SIGTERM → clean daemon shutdown)."""
    local = Path(sys.executable).parent / "foldyard"
    return [str(local) if local.exists() else "foldyard", "host"]


def _config_pin_check() -> tuple[str, str, str]:
    """Doctor row: is the checkout's ``foldyard.toml`` the one the host adopted? The pin makes a
    repo edit inert by design (:mod:`foldyard.configpin`), so the drift needs a PULL surface too —
    the supervisor's log line and its one-shot notification are easy to miss, and "my config change
    did nothing" is otherwise indistinguishable from a bug. WARN, never fail: the posture is safe
    either way; what's outstanding is a human decision."""
    cfg = worktree_config(config.active_worktree())
    try:
        drift = configpin.inspect(cfg)
    except OSError as e:  # pragma: no cover — unreadable state dir
        return _result(None, "adopted config", "", f"couldn't read the adopted copy: {e}")
    if not drift.pinned_exists:
        return _result(
            None, "adopted config", "", "nothing adopted yet — `fy up` / `fy host` adopts it once"
        )
    return _result(
        True if not drift.changed else None,  # None ⇒ WARN: safe posture, outstanding decision
        "adopted config",
        f"foldyard.toml matches what the host runs ({drift.tree_digest()})",
        "the checkout's foldyard.toml differs from the copy the host RUNS — `fy config diff`, "
        "then `fy config adopt` or `fy config revert`",
    )


def _widenings_check() -> tuple[str, str, str]:
    """Doctor row: what the ADOPTED config asks the host to allow (see :mod:`foldyard.exposure`).
    Green rows carry the counts — the point is that ``passthrough = ["@all"]`` silently means ~200
    un-decrypted hosts — and it WARNS only for config that reads as a control and isn't (a
    ``[proxy] allow`` list nothing consumes, a bundle ref that matches no bundle)."""
    from . import exposure

    cfg = worktree_config(config.active_worktree())
    try:
        with config.using(cfg):
            row = exposure.doctor_row(exposure.collect(cfg, read(apply_expiry=True)["mode"]))
    except Exception as e:  # pragma: no cover — a report must never break the doctor run
        return _result(None, "config widenings", "", f"couldn't inventory the config: {e}")
    ok, name, detail = row
    return _result(ok, name, detail, detail)


def doctor(deep: bool = False):
    """Setup checks, yielded one at a time as (status, name, detail).

    status is ok|warn|fail, or 'running' — a placeholder emitted right before a
    networked check so a live UI can show a spinner on that line until the real result
    replaces it (keyed by name). The box-side checks are the spine's own; the Mac-side
    "what can this machine grant?" probes are PLUGIN-contributed (each plugin yields its
    own gcloud/gh/PAM/impersonation rows). deep=True adds the live IAM probes. A generator
    so callers render progressively; `list(doctor(...))` still collects everything.
    """
    # Shadowing hygiene for the ACTIVE checkout — the box half is a pure filesystem scan, the
    # stack half asks the running containers. Same answer and same fix on either side of the
    # mount, so both are yielded before the split rather than duplicated into each branch.
    yield from _version_window_check()
    yield from _shadow_volume_check()
    yield from _stack_shadow_check()
    yield from _disk_headroom_check()

    if in_box():
        state = read()
        written = _parse(state["written"] or "")
        age = int((now() - written).total_seconds()) if written else None
        yield _result(
            None if age is None else age < 15,
            "mode mirror",
            f"fresh ({age}s old — supervisor heartbeat live)",
            "stale/missing — is `fy host` running on the Mac?",
        )
        yield _result(
            Path("/etc/dev-proxy-ca.pem").exists() or None,
            "egress proxy CA",
            "mounted + trusted (ambient — routing only in a github/capture mode)",
            "not mounted (no CA on the Mac yet — run `fy host`)",
        )
        yield ("running", "engine socket", "")
        rc, _ = _run([config.engine(), "info", "--format", "ok"], timeout=5)
        yield _result(rc == 0, "engine socket", "reachable", "unreachable — DOCKER_HOST broken?")
        # Everything above reads state the box was HANDED. These probe whether the posture is
        # actually reaching the box — the case the mirror can't see, because a rule whose mint
        # fails host-side still leaves the axis "on" and the proxy daemon "up".
        ctx = DoctorContext(deep=deep, run=_run, which=_which, result=_result, probe=probe)
        yield from registry().box_doctor_checks(ctx)
        return

    # Mac-side: one generic check, then each plugin's "what can this machine grant?" probes
    # (gcloud/ADC/PAM/impersonations from gcp; gh/mitmproxy/CA/host.env/PEM from github).
    # ctx hands the plugins devmode's own _run/_which/_result so their subprocess calls
    # still hit the redacting command log and render identically.
    ctx = DoctorContext(deep=deep, run=_run, which=_which, result=_result, probe=probe)
    yield _result(_which("uv"), "uv", "installed", "missing — brew install uv (TUI, data tooling)")
    yield _config_pin_check()
    yield _widenings_check()
    yield from _podman_checks()
    yield from registry().doctor_checks(ctx)


# Dependency trees a toolchain writes INTO the checkout — the dirs that need shadowing. Some
# names are ambiguous outside their ecosystem (`target` is a Rust build dir but also a perfectly
# ordinary folder name), so those carry a sibling-manifest guard: a hit only counts when the
# marker file sits next to it. Keeps the check quiet enough that a warn always means something.
_ARTIFACT_DIRS: dict[str, str] = {
    ".venv": "",
    "venv": "",
    "node_modules": "",
    "target": "Cargo.toml",
    ".next": "package.json",
}
_SCAN_MAX_DEPTH = 5  # `a/b/c/node_modules` is depth 4 in a monorepo; one level of headroom
_SCAN_PRUNE = {".git", ".jj", ".hg", ".svn"}


def _artifact_dirs(checkout: Path) -> list[str]:
    """EVERY checkout-relative dependency tree that exists on disk — the raw material both
    shadow checks work from (the box check subtracts what ``[box].shadow_volumes`` declares; the
    stack check subtracts what each container's volumes already mask).

    Walked by hand rather than with ``rglob`` so we can PRUNE: never descend into a match (a
    venv's own vendored node_modules is not a second finding), into VCS metadata, or past
    ``_SCAN_MAX_DEPTH``. ``os.scandir`` rather than ``Path.iterdir`` because its cached ``d_type``
    answers is-a-dir with NO extra syscall — the difference is 1.9s vs 0.25s on a monorepo over
    the box's virtiofs mount, where every stat is a two-hop round trip. Worth the lower-level
    code: doctor is the fast path and the TUI re-runs it on a timer."""
    found: list[str] = []
    root = str(checkout)

    def walk(d: str, depth: int) -> None:
        try:
            entries = list(os.scandir(d))
        except OSError:
            return  # unreadable dir (permissions, a race with a build) — not our business
        for e in entries:
            if e.name in _SCAN_PRUNE or not e.is_dir(follow_symlinks=False):
                continue
            marker = _ARTIFACT_DIRS.get(e.name)
            if marker is not None and (not marker or Path(d, marker).exists()):
                found.append(Path(e.path).relative_to(root).as_posix())
                continue  # matched (covered or not): never descend into a dependency tree
            if depth < _SCAN_MAX_DEPTH:
                walk(e.path, depth + 1)

    walk(root, 1)
    return sorted(found)  # scandir order is arbitrary; the rows must be stable across runs


def _shadow_volume_check():
    """The box mounts the checkout, so an in-tree dependency dir is the SAME directory the host
    builds into — two toolchains, two kernels, two uids, one dir, and they corrupt each other's
    installs. ``[box].shadow_volumes`` masks each one with a per-box named volume; nothing else
    detects the omission, because both sides just see a tree that keeps going wrong. WARN, never
    fail: sharing may be deliberate, and a project mid-setup shouldn't get a red row for it."""
    checkout = config.repo_root()
    covered = {d.strip("/") for d in config.box_shadow_volumes()}
    exposed = [d for d in _artifact_dirs(checkout) if d not in covered]
    if not exposed:
        detail = f"{len(covered)} shadowed, none exposed" if covered else "none in the checkout"
        yield _result(True, "shadow volumes", detail, "")
        return
    # Wrapped, not truncated: a monorepo can expose a dozen dirs, and the whole point of the row
    # is that the list IS the fix — a "(+9 more)" would leave the reader to re-derive it.
    import textwrap

    listed = textwrap.fill(
        ", ".join(f'"{d}"' for d in exposed),
        width=88,
        initial_indent=" " * 10,
        subsequent_indent=" " * 10,
        break_long_words=False,
        break_on_hyphens=False,
    )
    n = len(exposed)
    yield _result(
        None,
        "shadow volumes",
        "",
        f"{n} dependency dir{'' if n == 1 else 's'} shared with the host — the two sides will"
        " corrupt each other's installs"
        "\n      → add to [box].shadow_volumes:\n" + listed + "\n"
        "        (+ [box].warmup to fill them — a fresh shadow volume is EMPTY; uv/pnpm also"
        " need a copy link mode)",
    )


def active_project() -> str:
    """The compose project name for the ACTIVE checkout — ``<prefix>`` for main, ``<prefix>-<wt>``
    for a worktree, matching what ``stack`` labels containers with. Derived from config alone so
    doctor never calls ``stack.resolve()``, which would provision a VM on a read-only path."""
    worktree = config.active_worktree()
    prefix = config.project_prefix()
    return f"{prefix}-{worktree}" if worktree else prefix


def _stack_mounts(project: str) -> list[tuple[str, list[tuple[str, str]], set[str]]]:
    """``(service, [(host_src, container_dest)], {masked_dests})`` for each RUNNING container of
    ``project``, from one ``ps`` + one batched ``inspect``.

    Scoped to the project label, which is what makes this worktree-correct: a worktree's stack is
    its own compose project (``<prefix>-<name>``) mounting its OWN checkout, so main's containers
    are invisible here and vice versa — otherwise every worktree would report main's paths.

    Only READ-WRITE binds count as exposure: a ``:ro`` mount of the checkout can't be clobbered by
    the container (Tangible's auth0-simulator mounts its harness read-only and rsyncs out of it,
    which is exactly right and must not be flagged). The devbox is skipped — it is the OTHER
    check's subject, and reporting it twice would just teach people to skim the rows."""
    engine, env = config.engine(), _engine_env()
    try:
        out = subprocess.run(
            [
                engine,
                "ps",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        if out.returncode != 0:
            return []
        names = [n for n in out.stdout.split() if n and not n.endswith("-devbox")]
        if not names:
            return []
        got = subprocess.run(
            [engine, "inspect", "--format", "json", *names],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        specs = json.loads(got.stdout) if got.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []  # engine unreachable / unparsable — doctor's own engine row covers that
    result = []
    for i, spec in enumerate(specs):
        # Name each row from the SPEC, not from the `ps` list by position: `inspect` promises
        # nothing about order or about echoing back every argument, and a silent off-by-one here
        # would pin one service's dependency dirs on another's name — a finding that sends the
        # reader to the wrong container. Docker prefixes it with a slash, podman doesn't. The
        # positional fallback keeps an engine that omits Name behaving as before, and the devbox
        # filter is re-applied because it can no longer be guaranteed by the `ps` list alone.
        name = str(spec.get("Name") or "").lstrip("/") or (names[i] if i < len(names) else "")
        if not name or name.endswith("-devbox"):
            continue
        binds, masked = [], set()
        for m in spec.get("Mounts") or []:
            dest = (m.get("Destination") or "").rstrip("/")
            if not dest:
                continue
            if m.get("Type") == "bind":
                if m.get("RW"):
                    binds.append((str(m.get("Source") or "").rstrip("/"), dest))
            else:
                masked.add(dest)  # a named/anonymous volume shadows whatever is under it
        result.append((name, binds, masked))
    return result


def _version_window_check():
    """The consumer's declared foldyard version window (:mod:`foldyard.compat`).

    Yields nothing when the repo declares neither bound — "this consumer has no opinion" is
    not a finding. Reuses the same policy the CLI gate applies, so the row and the refusal can
    never disagree; the three outcomes map onto ok/warn/fail. Asks for the nudge
    unconditionally, ignoring ``FOLDYARD_NO_VERSION_NUDGE``: that variable silences a
    per-invocation nag, and someone running `fy doctor` is asking to be told everything.
    """
    minimum = config.min_foldyard_version()
    recommended = config.recommended_foldyard_version()
    if minimum is None and recommended is None:
        return

    from . import __version__
    from .compat import version_gate

    message, blocked = version_gate(__version__, minimum, recommended, in_box=in_box())
    fix = "`fy box up` from the Mac" if in_box() else "`uv tool install --upgrade foldyard`"
    if blocked:
        yield ("fail", "foldyard version", f"{__version__} — repo needs >= {minimum}; run {fix}")
    elif message:
        # Either behind the recommendation, or a version we could not order at all (a local
        # dev build, a bare source import). Both are "worth knowing, not worth blocking".
        want = recommended or minimum
        yield ("warn", "foldyard version", f"{__version__} — repo expects {want}; run {fix}")
    else:
        yield ("ok", "foldyard version", f"{__version__} satisfies this repo's declared window")


def _disk_headroom_check():
    """The VM's disk, asked of the engine so the answer is the same from either side of the
    mount (a host-side ``df`` would measure the Mac's disk instead of the store's).

    Yields nothing when the figure can't be read — no machine yet, a docker engine, a stopped
    VM — because "unknown" is not a finding. The threshold is stack's, shared with the ``fy
    up`` reclaim, so this row and that sweep can never disagree about what "low" means.

    Probed like every other read-only engine call in this module: through ``_engine_env`` (bare,
    it would miss a lima backend, which registers no default podman connection) and on the short
    probe budget, so a hung engine can't stall a TUI refresh."""
    from . import stack

    head = stack.disk_headroom(_engine_env(), timeout=5)
    if head is None:
        return
    yield _result(
        None if head.low else True,
        "engine disk",
        f"{head.render()} on the image/volume store",
        f"{head.render()} — a build can die mid-layer on 'no space left on device'. "
        f"`fy up` reclaims automatically at this level; to sweep now: "
        f"{config.engine()} image prune -f --filter until={stack.RECLAIM_MIN_AGE}",
    )


def _stack_shadow_check():
    """The stack half of the shadow story: a compose service that bind-mounts part of the checkout
    shares its dependency dirs with the host exactly like an unshadowed box does, and no config
    file states that — only the running container knows. So ask IT, rather than parsing compose
    (podman-compose has no ``config --format json``, and foldyard stays stdlib-only on this path).

    Yields nothing when the stack isn't up: a stack-less project must not get a row about a stack,
    and "no containers" is not a finding. Which is why the 'running' placeholder is emitted only
    AFTER the containers are known — doctor's contract is that every placeholder is resolved by a
    later real row of the same name, and an unconditional one would leave a live UI spinning
    forever on a project that has no stack at all (pinned by test_tui's generator test)."""
    containers = _stack_mounts(active_project())
    if not containers:
        return
    yield ("running", "stack shadowing", "")  # the checkout scan below — let a live UI spin
    checkout = config.repo_root()
    dirs = _artifact_dirs(checkout)
    exposed: dict[str, list[str]] = {}
    for service, binds, masked in containers:
        for src, dest in binds:
            for rel in dirs:
                host = f"{checkout}/{rel}"
                if host != src and not host.startswith(src + "/"):
                    continue
                inside = dest + host[len(src) :]
                # A volume ON the path or on any PARENT of it shadows the dir — `/w:vol` protects
                # `/w/.venv` just as `/w/.venv:vol` does. Walk up rather than test equality.
                probe = inside
                while probe and probe != "/":
                    if probe in masked:
                        break
                    probe = probe.rsplit("/", 1)[0]
                else:
                    exposed.setdefault(rel, []).append(service)
    if not exposed:
        n = len(containers)
        yield _result(True, "stack shadowing", f"{n} container{'' if n == 1 else 's'} clean", "")
        return
    lines = "\n".join(
        f"          {rel}  →  {', '.join(sorted(set(svcs)))}"
        for rel, svcs in sorted(exposed.items())
    )
    n = len(exposed)
    yield _result(
        None,
        "stack shadowing",
        "",
        f"{n} dependency dir{'' if n == 1 else 's'} left on the host mount by running services"
        "\n      → give each a named volume at that path in the service's compose `volumes:`\n"
        + lines,
    )


def _podman_checks():
    """Core (not plugin) Mac-side checks the whole stack rests on: the podman CLI is
    installed, and its machine is up (or at least ready to start). Mac-only — the box has no
    podman (it IS the machine's guest), and doctor's in_box() branch returns before this. The
    machine check is a WARN (not a fail) when stopped/uninitialised: that's the expected
    pre-`fy up` state, and the TUI's `s` key (or `fy up`) brings it up from there."""
    have_podman = _which("podman")
    yield _result(
        have_podman,
        "podman CLI",
        "installed",
        "missing — brew install podman (the dev stack's container engine)",
    )
    if not have_podman:
        return  # nothing to inspect without the CLI; the row above already flagged it
    # Lima backend additionally needs limactl on the host to manage the VM.
    if _BACKEND.name != "podman":
        ok = _BACKEND.available()
        yield _result(
            ok,
            f"{_BACKEND.cli} CLI",
            "installed",
            f"missing — backend = '{_BACKEND.name}' needs it ({_BACKEND.install_hint})",
        )
        if not ok:
            return
    machine = PODMAN_MACHINE
    label = f"{_BACKEND.name} machine"
    state = machine_state()  # 'running' | 'stopped' | 'unknown' (unknown ⇒ not initialised)
    if state == "running":
        yield _result(True, label, f"{machine} running", "")
    elif state == "stopped":
        yield _result(None, label, "", f"{machine} stopped — `fy up` (or press s) starts it")
    else:
        yield _result(None, label, "", f"{machine} not initialised yet — `fy up` creates it")


def doctor_cli(deep: bool) -> int:
    marks = {"ok": "\033[32m✓\033[0m", "warn": "\033[33m○\033[0m", "fail": "\033[31m✗\033[0m"}
    print(
        f"Doctor — {'box' if in_box() else 'Mac'} setup checks"
        + (" (deep: live IAM probes)" if deep else " (fast; `doctor deep` adds live IAM probes)")
    )
    worst = 0
    for status, name, detail in doctor(deep):
        if status == "running":
            continue  # live-UI spinner placeholder; the real result follows
        print(f"  {marks[status]} {name:<28} {detail}")
        worst = max(worst, {"ok": 0, "warn": 0, "fail": 1}[status])
    return worst


# ── the dashboard ──────────────────────────────────────────────────────────────────


def _countdown(iso: str) -> str:
    exp = _parse(iso)
    if exp is None:
        return "?"
    left = int((exp - now()).total_seconds())
    return f"{left // 60}m{left % 60:02d}s" if left > 0 else "EXPIRED"


def _box_env_hint(mode: dict, project: str | None = None) -> str | None:
    """Does the running dev box's env match the mode? (best effort, via the engine)"""
    box = (project or os.environ.get("PODMAN_PROJECT") or config.project_prefix()) + "-devbox"
    try:
        out = subprocess.run(
            [config.engine(), "inspect", "-f", "{{json .Config.Env}}", box],
            capture_output=True,
            text=True,
            timeout=5,
            env=_engine_env(),
        )
        if out.returncode != 0:
            return None  # box not running (or no engine here) — nothing to compare
        env = dict(e.split("=", 1) for e in json.loads(out.stdout) if "=" in e)
    except Exception:
        return None
    # NB gcp metadata + the egress proxy env are baked UNCONDITIONALLY now: the box ALWAYS points
    # GCE_METADATA_HOST at the on-network emulator, and Phase A′ ALWAYS routes egress through the
    # proxy (HTTPS_PROXY always set). Their presence no longer tracks the mode — the rung's enforced
    # entirely Mac-side (the minter up/down, the proxy's host-side rules), reconciled live by
    # `fy host`, so changing gcp/github/capture needs NO `fy box up`. (Checking them here mis-fired
    # "gcp metadata out of date" forever, since gcp defaults to off but the host is always baked.)
    # What DOES still need a box rebuild is a keyless injector toggling: it bakes (or drops) a DUMMY
    # credential the client must emit for the proxy to rewrite. Detect that via the env var the
    # keyless KIND bakes — claude (oauth/api-key) and codex api-key. Codex `chatgpt` is file-based
    # (~/.codex/auth.json, not Config.Env), so it's intentionally not checked here (no false alarm).
    from . import keyless

    mismatches = []
    for label, kind, taxonomy, axis in (
        ("claude keyless", config.claude_keyless(), keyless.CLAUDE_KEYLESS, "claude"),
        ("codex keyless", config.codex_keyless(), keyless.CODEX_KEYLESS, "codex"),
    ):
        spec = taxonomy.get(kind) if kind else None
        if spec and (mode.get(axis, "off") != "off") != bool(env.get(spec["env"])):
            mismatches.append(label)
    if mismatches:
        return f"⚠ dev box env out of date ({', '.join(mismatches)}) — apply with: fy box up"
    return None


def show() -> int:
    state = read()
    mode, expires = state["mode"], state["expires"]

    if in_box():
        # The mirror is informational; daemon status comes from the supervisor's
        # heartbeat, with a live cross-check against the host where possible.
        written = _parse(state["written"] or "")
        age = int((now() - written).total_seconds()) if written else None
        stale = age is None or age > 15
        print(
            f"Dev posture (mirror: {state['source']}"
            + (f", {age}s old" if age is not None else "")
            + ")"
        )
        daemons = state["daemons"] or {}
    else:
        print(f"Dev posture (authoritative: {state['source']})")
        stale = False
        daemons = daemon_status(mode)

    defaults = axis_defaults()
    if all(v == defaults[a] for a, v in mode.items()):
        print("  fully offline — emulators only, zero secrets (the default)")

    # Capability-probe results (plugins' capability_probes, run by the supervisor): the box's
    # slice rides its mirror; the host reads the project state file keyed by worktree. A missing
    # entry is "no claim" — only a probed-and-failing capability marks an axis DEGRADED.
    degraded = dict(degraded_capabilities(mode))

    blurb = mode_blurb()
    axis_daemons = axis_daemon()
    emergency_rungs = emergency()
    for axis in axes():
        value = mode[axis]
        line = f"  {axis:<7} {value:<5} {blurb[(axis, value)]}"
        if axis in expires:
            line += f"   [expires in {_countdown(expires[axis])}]"
        if axis in degraded:
            line += f"   ⚠ DEGRADED — {degraded[axis]}"
        dn = axis_daemons.get(axis)  # axis → its daemon name (plugin-declared); None ⇒ no daemon
        daemon = daemons.get(dn) if dn else None
        if value != defaults[axis] and dn:
            if daemon:
                up = daemon.get("up")
                mark = "● up" if up else "○ DOWN — run `fy host` on the Mac"
                if stale:
                    mark += " (status stale — supervisor heartbeat missing?)"
                line += f"   [{daemon.get('label', '')}: :{daemon.get('port')} {mark}]"
            else:
                line += "   [daemon status unknown]"
        print(line)

    if any(mode[axis] in emergency_rungs.get(axis, ()) for axis in axes()):
        print("  ⚠ EMERGENCY user-credential mode is ON — auto-reverts at expiry.")

    # The stored state can drift incoherent without a set (a TTL expiry reverts one axis while a
    # dependent one persists) — set_mode would refuse this combination, so surface it here too.
    for sev, msg in registry().mode_issues(mode):
        print(f"  {'✗' if sev == 'error' else '⚠'} {msg}")

    hint = _box_env_hint(mode)
    if hint:
        print(f"  {hint}")
    if not in_box():
        wanted = desired_daemons(mode)
        if wanted and not all(d["up"] for d in daemons.values()):
            print("  start the host daemons:  fy host        (one terminal, Ctrl-C stops)")
        # Built from the LIVE axes (not a fixed string) so the advertised commands always match the
        # active registry — a different consumer/worktree can have a different axis set.
        axis_spec = " ".join(f"{axis}={'|'.join(rungs)}" for axis, rungs in axes().items())
        print(f"  change with:  fy mode {axis_spec} [ttl=1h]")
    return 0


# ── the test clock (fy clock — TTL machinery without the waiting) ───────────────────


def clock_cli(args: list[str]) -> int:
    """``fy clock`` — show/skew the posture clock (TESTING aid; docs/testing-modes.md).

    ``fy clock`` shows the current skew; ``fy clock ff 2h`` fast-forwards every devmode reader
    (TTL expiry, countdowns, the supervisor's revert+settle) by writing the host-side offset
    file; ``fy clock reset`` returns to real time. Host-only for writes — the box must not skew
    the host's view of time (the offset lives in the Mac home, like the mode file)."""
    offset = clock_offset()
    if not args:
        skew = f"+{int(offset)}s" if offset else "none (real time)"
        print(f"posture clock skew: {skew}   now(): {_iso(now())}")
        if offset:
            print("  reset with: fy clock reset")
        return 0
    if in_box():
        raise SystemExit("✗ clock changes are host-only (the box must not skew the host clock).")
    if args[0] == "reset":
        config.clock_offset_file().unlink(missing_ok=True)
        print(f"✓ posture clock reset to real time (now(): {_iso(now())})")
        return 0
    if args[0] == "ff" and len(args) == 2:
        text = args[1].strip().lower()
        mult = {"s": 1, "m": 60, "h": 3600}.get(text[-1:], None)
        try:
            seconds = int(text[:-1]) * mult if mult else int(text)  # unclamped, unlike parse_ttl
        except ValueError:
            raise SystemExit(f"✗ can't parse duration {args[1]!r} (use 90s / 30m / 2h)") from None
        if seconds <= 0 or seconds + offset > _MAX_CLOCK_OFFSET:
            raise SystemExit(
                f"✗ fast-forward must be positive and keep the total skew under "
                f"{int(_MAX_CLOCK_OFFSET)}s: {args[1]!r}"
            )
        path = config.clock_offset_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{int(offset) + seconds}\n")
        print(f"✓ posture clock fast-forwarded {text} (total skew +{int(offset) + seconds}s)")
        print("  the supervisor picks this up within a tick; `fy clock reset` undoes it.")
        return 0
    raise SystemExit(f"usage: foldyard clock [ff <90s|30m|2h>|reset] (got {' '.join(args)!r})")


# ── CLI ────────────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "show"
    if cmd == "show":
        return show()
    if cmd == "set":
        updates: dict[str, str] = {}
        ttl: int | None = None
        for arg in argv[1:]:
            if "=" not in arg:
                raise SystemExit(f"✗ expected axis=value or ttl=…, got {arg!r}")
            key, value = arg.split("=", 1)
            if key == "ttl":
                ttl = parse_ttl(value)
            else:
                updates[key] = value
        if not updates:
            raise SystemExit("✗ nothing to set (e.g. `fy mode gcp=logs github=app ttl=1h`)")
        set_mode(updates, ttl)
        return show()
    if cmd == "env":
        state = read()
        for key, value in derive_env(state["mode"]).items():
            print(f'export {key}="${{{key}:-{value}}}"')
        return 0
    if cmd == "doctor":
        return doctor_cli(deep="deep" in argv[1:])
    if cmd == "clock":
        return clock_cli(argv[1:])
    if cmd == "workspaces":
        for ws in workspaces():
            stack = "engine?" if ws["containers"] is None else f"{ws['containers']} containers"
            box = "devbox up" if ws["devbox"] else "no devbox"
            print(f"  {ws['name']:<16} {ws['branch']:<28} {stack:<16} {box}   {ws['path']}")
        return 0
    raise SystemExit(
        f"usage: foldyard mode [show|set axis=value… [ttl=…]|env|doctor [deep]|clock|"
        f"workspaces] (got {cmd!r})"
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
