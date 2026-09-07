"""`fy state` — every state tier's desired vs observed posture, in one read-only view.

A thin renderer: the tiers themselves — what "desired" and "observed" mean per scope, and
each scope's action — live in :mod:`foldyard.reconcile` (the scope inventory, consolidation
proposal A). This module only formats :class:`~foldyard.reconcile.ScopeRow` lines and turns
any drift into a non-zero exit, so scripts can gate on it. Any scope that can't be observed
says "unknown" rather than guessing. Works host-side and (with the box-visible subset) in
the box. Stdlib only.
"""

from __future__ import annotations

from . import config, devmode, reconcile


def show() -> int:
    state = devmode.read()
    where = "box" if devmode.in_box() else "host"
    wt = config.active_worktree() or "main"
    print(f"State — desired vs observed, per tier ({config.project_prefix()}, {wt}, {where})")
    marks = {"ok": "\033[32m✓\033[0m", "drift": "\033[31m✗\033[0m", "unknown": "\033[33m○\033[0m"}
    worst = 0
    for scope in reconcile.scopes():
        if scope.host_only and devmode.in_box():
            continue
        for row in scope.rows(state):
            print(f"  {marks[row.status]} {row.scope:<11} {row.desired}")
            print(f"    {'':<11} → {row.observed}")
            worst = max(worst, {"ok": 0, "unknown": 0, "drift": 1}[row.status])
    if worst:
        print("  (✗ = a tier disagrees with the desired posture — the fix is in its row)")
    return worst


def main(argv: list[str]) -> int:
    if argv:
        raise SystemExit(f"usage: foldyard state (got {' '.join(argv)!r})")
    return show()
