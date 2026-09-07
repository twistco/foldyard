"""Cross-project Mac-side daemon port-band allocation.

Every foldyard project runs its own Mac-side daemons (egress proxy, gcp minter), and every
consumer of a daemon's port — the supervisor's listener, the box's ``FY_PROXY`` env, the VM
wall's nft allow-ranges, the in-box probes — must agree on it *and* it must be stable across
restarts (it's baked into box env and VM ``environment.d`` at create time). Two projects on
one Mac therefore need DISJOINT ports: with a single global base, both supervisors held their
own per-project singleton lock, both bound the same port, and each reaped the other's proxy
as "orphaned" every tick — an endless fight (the 2026-07 boot-loop).

So each project gets a 200-port BAND, allocated first-come from a tiny flock-guarded registry
(``~/.foldyard/ports.json``) and reused forever after:

  base + 0..89     egress-proxy listeners (the 1..89 worktree-offset span; main is +0)
  base + 100..189  gcp-minter listeners   (same span)

Bands start at 41000 — well away from the crowded 8xxx dev-server neighbourhood, and below
the macOS ephemeral range (49152+) so the OS never hands our ports to outbound connections.
Allocation is REGISTRY-ONLY (no live socket probing): deterministic, and foldyard-vs-foldyard
is the collision that actually happens. A foreign service squatting a band is handled where
it always was — the supervisor's foreign-listener nag — and ``FY_PROXY_PORT``/
``GCP_MINTER_PORT`` env stay as explicit overrides that bypass allocation entirely.

Stale entries (deleted projects) are harmless — 40 bands is plenty — and can be pruned by
editing the registry file.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path

BAND = 200  # ports per project: two daemons × the 0..89 worktree-offset span, with headroom
FIRST_BASE = 41000
LAST_BASE = 48800  # last full band ends at 48999, below the macOS ephemeral range (49152+)
PROXY_SLOT = 0  # egress-proxy base within the band
MINTER_SLOT = 100  # gcp-minter base within the band


def registry_file() -> Path:
    """The cross-project registry. ``FY_PORTS_FILE`` overrides (tests point it at a tmp file so
    no test ever touches the real one); the default is deliberately NOT under a project's
    ``state_dir`` — the whole point is that it spans projects."""
    env = os.environ.get("FY_PORTS_FILE")
    return Path(env).expanduser() if env else Path.home() / ".foldyard" / "ports.json"


def project_base(project: str) -> int:
    """The project's allocated band base — allocating (and announcing) it on first use.
    flock-guarded so two concurrent ``fy up``s in different projects can't claim one band."""
    path = registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        try:
            reg = json.loads(fh.read() or "{}")
        except ValueError:
            reg = {}
        if not isinstance(reg, dict):
            reg = {}
        base = reg.get(project)
        if isinstance(base, int):
            return base
        taken = {v for v in reg.values() if isinstance(v, int)}
        for cand in range(FIRST_BASE, LAST_BASE + 1, BAND):
            if cand in taken:
                continue
            reg[project] = cand
            fh.seek(0)
            fh.truncate()
            json.dump(reg, fh, indent=1, sort_keys=True)
            print(
                f"ℹ allocated Mac daemon ports {cand}-{cand + BAND - 1} to project "
                f"'{project}' ({path})",
                file=sys.stderr,
            )
            return cand
    raise RuntimeError(
        f"✗ no free daemon port band left in {FIRST_BASE}-{LAST_BASE + BAND - 1} — prune "
        f"stale projects from {path} (or set FY_PROXY_PORT/GCP_MINTER_PORT explicitly)."
    )
