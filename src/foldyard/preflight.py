"""Preflight — hard-prerequisite checks run BEFORE `fy up` / `fy box up` (host-only).

A stored posture is not a running daemon, and a declared backend is not an installed CLI. The box
routes ALL egress through the Mac's mitmdump proxy (Phase A′) whenever any injector/keyless or a
`[proxy]` table is active, so a missing host tool or an un-routable backend leaves a freshly-built
box unable to reach even PyPI at bootstrap — which used to surface only as a cryptic
`tunnel error / Connection refused` deep inside the box's bootstrap, long after `✓ up` printed.

This module turns those into an early, actionable ABORT before anything is built or started. It's
host-only (the checks concern the Mac's tools/posture; the box neither manages the machine nor runs
the proxy), so :func:`check_or_abort` no-ops in the box. Stdlib + a lazy plugin import, so it stays
cheap on the up path.
"""

from __future__ import annotations

import sys
from shutil import which

from . import config, devmode, machine_backend


def _proxy_required(mode: dict) -> bool:
    """Would this project's box route egress through the Mac proxy? Must MIRROR the proxy plugin's
    ``derive_env`` routing signal exactly — ``config.proxy_enabled()`` OR any active injection rule
    (``registry().proxy_rules``, which aggregates github, keyless Claude/Codex, AND generic
    ``[[inject]]`` axes). An earlier hand-rolled list checked only proxy/keyless/github and MISSED
    ``[[inject]]``: an inject-only box then skipped the mitmproxy-missing check (cryptic PyPI
    connect-refuse at bootstrap) and, under ``[machine].wall``, got wrongly aborted as 'nothing
    routes through the proxy'. When true, a box with no reachable proxy can't reach ANYTHING — so
    the proxy's prerequisites (mitmproxy installed, an address that routes to the Mac) are HARD."""
    if config.proxy_enabled() or config.claude_keyless() != "" or config.codex_keyless() != "":
        return True
    from .plugins import registry  # lazy: plugins load on the hot path

    return bool(registry().proxy_rules(mode))


def issues() -> list[str]:
    """Every blocking prerequisite problem for `up`/`box up`, each a ready-to-print block with a fix
    hint. Empty ⇒ good to go. Reads config + host PATH + posture; no side effects."""
    out: list[str] = []

    # 1. The ENGINE CLI drives the socket every backend hands out, so it is required whatever the
    #    backend is — and it is a DIFFERENT binary from the VM-lifecycle one (lima needs limactl
    #    AND podman). `config.engine()` resolves to docker only when podman is absent, and that
    #    fallback is for CI (ADR-0010), so a resolved `docker` on a dev host means podman is
    #    missing — hence the fix hint names podman, not "podman or docker".
    if not which(config.engine()):
        out.append(
            f"✗ the container engine `{config.engine()}` isn't installed — every stack, box and\n"
            "    compose verb shells out to it. Install podman (`brew install podman`); it is\n"
            "    also what drives the VM's socket under the lima backend."
        )

    # 2. The backend's own host CLI must exist too, or the machine can't be created/targeted —
    #    and that blocks `up` whether the consumer NAMED the backend or inherited the default.
    #    The inherited case gets its own block because the fix is a different one (see
    #    machine_backend.default_unavailable_block: skipping it silently is how the stack ends
    #    up on the host's podman socket with the VM boundary gone). Mirrors machine.ensure().
    backend_name = config.machine_backend()
    backend = machine_backend.get_backend(backend_name)
    if not backend.available():
        out.append(
            f"✗ the '{backend_name}' backend needs `{backend.cli}`, which isn't installed.\n"
            f"    Install it ({backend.install_hint}) or change [machine].backend."
            if config.machine_backend_explicit()
            else machine_backend.default_unavailable_block(backend)
        )

    # 3. An unrecognised keyless value normalises to "" — keyless SILENTLY off, a real footgun. Flag
    #    the raw value so a typo (`keyless = "oath"`) fails loudly instead of shipping a login box.
    for table, normalised, allowed in (
        ("claude", config.claude_keyless(), "api-key | oauth | true"),
        ("codex", config.codex_keyless(), "api-key | chatgpt | true"),
    ):
        raw = config._table(table).get("keyless")
        if raw not in (None, False, True) and normalised == "":
            out.append(
                f"✗ [{table}].keyless = {raw!r} isn't recognised — keyless would be OFF.\n"
                f"    Use one of: {allowed} (or remove the line)."
            )

    # 4. When the box will route through the proxy, the proxy must be able to RUN and be REACHABLE.
    mode = devmode.read()["mode"]
    if _proxy_required(mode):
        from .plugins.proxy import mitmdump_path

        if mitmdump_path() is None:
            out.append(
                "✗ this project routes box egress through the Mac's mitmdump proxy, but mitmproxy\n"
                "    isn't installed on the host — the box's egress (even PyPI at bootstrap) will\n"
                "    connection-refuse. Install it: `just foldyard install` (the [host] extra)."
            )

    # 5. A foldyard.toml INSIDE a bigger git repo (e.g. running an example in-place from the
    #    foldyard checkout) is a NESTED project: config binds the marker dir, but the CHECKOUT
    #    (machine mounts, worktrees, FOLDYARD_CHECKOUT, the env override) resolves via git to the
    #    ENCLOSING repo — so `up` would mount the whole outer repo into this project's VM, and a
    #    checkout narrowed to the marker dir would leave the box a .git-less tree (no in-box
    #    commits). Unsupported: abort with the copy-out recipe instead of silently binding the
    #    outer repo. Pure-filesystem (a .git above the marker, none at it — a .git FILE counts:
    #    that's the project's own linked worktree), so no subprocess on the up path.
    root = config.repo_root()
    if (root / "foldyard.toml").exists() and not (root / ".git").exists():
        enclosing = next((p for p in root.resolve().parents if (p / ".git").exists()), None)
        if enclosing is not None:
            out.append(
                f"✗ this project's foldyard.toml ({root}) sits INSIDE another git repo\n"
                f"    ({enclosing}). A foldyard project must be its own git checkout (the machine\n"
                "    mounts it, the box commits in it), so running nested would bind the\n"
                "    ENCLOSING repo. Copy it out and make it a repo, e.g.:\n"
                f"      cp -r {root} ~/my-project && cd ~/my-project &&\n"
                "      git init && git add -A && git commit -m init"
            )

    # 6. The active worktree's port offset must fit 0..89. Daemon ports are band_base+offset
    #    (proxy) and band_base+100+offset (minter — ports.py), and the wall opens exactly those
    #    two 90-port spans. The cksum default is 1..89, but an explicit WT_OFFSET env or a
    #    foldyard.local.toml pin is taken verbatim: a pin ≥90 puts the proxy outside the wall's
    #    opened range (total egress outage while the daemon looks healthy) or into the minter
    #    span (the supervisor's endless can't-bind nag); a negative pin underflows the band.
    wt = config.active_worktree()
    if wt:
        offset = config.worktree_offset(wt)
        if not 0 <= offset <= 89:
            out.append(
                f"✗ worktree '{wt}' has port offset {offset}, outside the required 0..89 — its\n"
                "    proxy/minter daemon ports would fall outside the band (and the wall's opened\n"
                "    ranges) or collide across daemons. Pin an offset ≤89 in foldyard.local.toml\n"
                "    [worktree-offsets] (or unset WT_OFFSET)."
            )

    # 7. The wall only exists for lima (podman-machine's CoreOS appliance can't be provisioned;
    #    native has no VM), and a walled VM with no proxy routing is an AIRGAPPED box — the wall
    #    default-denies direct egress, so without the proxy there is no way out at all.
    if config.machine_wall():
        if backend_name != "lima":
            out.append(
                f"✗ [machine].wall = true needs the lima backend, but backend = '{backend_name}'.\n"
                '    Set [machine].backend = "lima" (the wall provisions nftables into the lima\n'
                "    VM), or drop `wall`."
            )
        if not _proxy_required(mode):
            out.append(
                "✗ [machine].wall = true but nothing routes the box through the egress proxy —\n"
                "    the wall default-denies direct egress, so the box would have NO way out.\n"
                "    Declare a `[proxy]` table (or keyless/an injector) so egress goes Mac-side,\n"
                "    or drop `wall`."
            )
    return out


def check_or_abort(context: str) -> None:
    """Print any blocking issues and ``raise SystemExit(1)``; a no-op in the box or when all clear.
    ``context`` names the action (e.g. ``"fy up"``) for the header."""
    if config.in_box():
        return
    problems = issues()
    if not problems:
        return
    print(f"✗ {context}: prerequisites not met —\n", file=sys.stderr)
    for p in problems:
        print(p, file=sys.stderr)
    print("\n  Fix the above (or adjust the backend/posture) and retry.", file=sys.stderr)
    raise SystemExit(1)
