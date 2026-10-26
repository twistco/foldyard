"""``just prune``: tidy up after merged PRs — the Claude Code worktrees under ``.claude/worktrees/``,
the local branches, and the per-checkout dev venvs nothing points at any more.

None of them goes on its own: Claude Code leaves a worktree (with its own ``.venv``) behind for each
isolated piece of work, a squash merge leaves the local branch (``git branch -d`` can't tell it was
merged), and the justfile's per-checkout ``UV_PROJECT_ENVIRONMENT`` outlives its checkout. By
2026-10-26 that was 24 worktrees, ~40 branches and 93 venvs, 68 of them for checkouts long gone.

A dry run unless ``--apply``. What counts as merged is that nothing on the branch is missing from
main (:func:`merged_by`): its tip is on origin/main, or is (or is behind) the head of a MERGED PR
from that branch — so a commit made after the merge keeps it. A worktree needs the PR: a fresh one
sits on main too. It goes only from ``.claude/worktrees/`` (a ``fy worktree`` checkout has
containers and a registry entry only ``fy worktree remove`` clears), clean, unlocked, and no
process's working directory. A venv goes when no remaining checkout's path names it — including
any other clone of foldyard on this computer, whose next ``just test`` rebuilds it from uv's cache.

Stdlib only: ``uv run --no-project`` runs it, so it never touches the project env it may delete.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

MAIN_BRANCH = "main"
MAIN_REF = f"origin/{MAIN_BRANCH}"
CLAUDE_WORKTREES = Path(".claude") / "worktrees"


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str | None  # None: detached
    locked: bool = False
    dirty: bool = False
    prunable: bool = False  # its directory is gone; git still has the branch checked out there


@dataclass(frozen=True)
class PR:
    number: int
    branch: str
    head: str


@dataclass
class Plan:
    worktrees: list[tuple[Worktree, str]] = field(default_factory=list)  # (worktree, merged by)
    branches: list[tuple[str, str]] = field(default_factory=list)  # (branch, merged by)
    venvs: list[Path] = field(default_factory=list)  # no checkout at all
    # each removed worktree's venv, deleted only once git has removed the worktree
    worktree_venvs: dict[Path, Path] = field(default_factory=dict)
    kept: list[tuple[str, str]] = field(default_factory=list)  # (what, why) — merged but kept


def merged_by(
    branch: str,
    tip: str,
    prs: Iterable[PR],
    is_ancestor: Callable[[str, str], bool],
    require_pr: bool = False,
) -> str | None:
    """What holds every commit on ``branch`` (``#<n>`` or ``main``), else None."""
    for pr in prs:
        if pr.branch == branch and is_ancestor(tip, pr.head):
            return f"#{pr.number}"
    if not require_pr and is_ancestor(tip, MAIN_REF):
        return MAIN_BRANCH
    return None


def plan(
    worktrees: list[Worktree],
    branches: dict[str, str],
    prs: list[PR],
    venvs: list[Path],
    is_ancestor: Callable[[str, str], bool],
    venv_name: Callable[[Path], str],
    busy: Callable[[Path], bool],
) -> Plan:
    """``worktrees`` as git lists them (the main checkout first), ``branches`` name → tip."""
    out = Plan()
    main, *others = worktrees
    remaining = [main]
    for wt in others:
        why = (
            merged_by(wt.branch, branches[wt.branch], prs, is_ancestor, require_pr=True)
            if wt.branch in branches and wt.path.parent == main.path / CLAUDE_WORKTREES
            else None
        )
        blocker = (
            "its directory is gone — run `git worktree prune`" if wt.prunable else
            "uncommitted changes" if wt.dirty else
            "locked" if wt.locked else
            "in use by a running process" if why and busy(wt.path) else
            None
        )  # fmt: skip
        if why and not blocker:
            out.worktrees.append((wt, why))
            continue
        if why and blocker:
            out.kept.append((str(wt.path), f"merged ({why}) but {blocker}"))
        remaining.append(wt)
    checked_out = {wt.branch for wt in remaining}
    for name, tip in sorted(branches.items()):
        if name == MAIN_BRANCH or name in checked_out:
            continue
        if why := merged_by(name, tip, prs, is_ancestor):
            out.branches.append((name, why))
    keep = {venv_name(wt.path) for wt in remaining}
    removing = {venv_name(wt.path): wt.path for wt, _ in out.worktrees}
    for v in venvs:
        if v.name in removing:
            out.worktree_venvs[removing[v.name]] = v
        elif v.name not in keep:
            out.venvs.append(v)
    return out


def apply(p: Plan, repo: Path) -> int:
    """Worktrees first (git refuses one that changed since the plan, and its branch and venv
    stay), then branches, then venvs. Returns 1 if anything was refused."""
    failed = 0
    refused: set[Worktree] = set()
    for wt, _ in p.worktrees:
        r = _git(repo, "worktree", "remove", str(wt.path), check=False)
        if r.returncode:
            failed = 1
            refused.add(wt)
            print(f"✗ kept {wt.path}: {r.stderr.strip()}", file=sys.stderr)
    refused_branches = {wt.branch for wt in refused}
    for name, _ in p.branches:
        if name in refused_branches:
            continue
        r = _git(repo, "branch", "-D", name, check=False)
        if r.returncode:
            failed = 1
            print(f"✗ kept branch {name}: {r.stderr.strip()}", file=sys.stderr)
    refused_paths = {wt.path for wt in refused}
    gone = [v for path, v in p.worktree_venvs.items() if path not in refused_paths]
    for v in [*p.venvs, *gone]:
        shutil.rmtree(v, ignore_errors=True)
    return failed


# ── reading this computer ─────────────────────────────────────────────────────────────────────


def parse_worktrees(text: str) -> list[Worktree]:
    """``git worktree list --porcelain``."""
    out = []
    for block in text.strip().split("\n\n"):
        fields = dict((line + " ").split(" ", 1) for line in block.splitlines())
        branch = fields.get("branch", "").strip().removeprefix("refs/heads/") or None
        path = Path(fields["worktree"].strip())
        out.append(Worktree(path, branch, locked="locked" in fields, prunable="prunable" in fields))
    return out


def parse_lsof_cwds(text: str) -> list[Path]:
    """``lsof -d cwd -F n``: every process's working directory, one ``n<path>`` line each."""
    return [Path(line[1:]) for line in text.splitlines() if line.startswith("n/")]


def inside(path: Path, cwds: Iterable[Path]) -> bool:
    return any(c == path or path in c.parents for c in cwds)


def venv_name(checkout: Path) -> str:
    """The justfile's ``dev-venv-$(echo "{{_dir}}" | cksum | cut -d' ' -f1)`` — the system cksum,
    for exact parity, as ``stack._offset`` does."""
    out = subprocess.run(["cksum"], input=f"{checkout}\n", capture_output=True, text=True)
    return f"dev-venv-{out.stdout.split()[0]}"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


def _cwds(proc: Path = Path("/proc")) -> list[Path] | None:
    """Every process's working directory, or None when this computer can't say. An empty listing
    is a failure, never "nothing is busy": this very process has a working directory."""
    try:
        r = subprocess.run(["lsof", "-d", "cwd", "-F", "n"], capture_output=True, text=True)
        if cwds := parse_lsof_cwds(r.stdout):
            return cwds
    except FileNotFoundError:
        pass
    if not proc.is_dir():
        return None
    out = []
    for link in proc.glob("[0-9]*/cwd"):
        try:
            out.append(Path(os.readlink(link)))
        except OSError:
            continue
    return out or None


def _merged_prs(repo: Path) -> list[PR]:
    try:
        r = subprocess.run(
            ["gh", "pr", "list", "--state", "merged", "--limit", "1000",
             "--json", "number,headRefName,headRefOid,isCrossRepository"],
            cwd=repo, capture_output=True, text=True, check=True,
        )  # fmt: skip
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"! no PRs from gh ({e}); only branches already on main count", file=sys.stderr)
        return []
    return [
        PR(p["number"], p["headRefName"], p["headRefOid"])
        for p in json.loads(r.stdout)
        if not p["isCrossRepository"]
    ]


def gather(repo: Path, cache: Path) -> Plan:
    if _git(repo, "fetch", "-q", "--prune", "origin", check=False).returncode:
        print(f"! git fetch failed; judging against the {MAIN_REF} already here", file=sys.stderr)
    worktrees = [
        wt
        if wt.prunable
        else replace(wt, dirty=bool(_git(wt.path, "status", "--porcelain").stdout))
        for wt in parse_worktrees(_git(repo, "worktree", "list", "--porcelain").stdout)
    ]
    branches = dict(
        line.split()
        for line in _git(
            repo, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads"
        ).stdout.splitlines()
    )
    prs = _merged_prs(repo)
    for pr in prs:  # a PR head whose remote branch is gone may never have been fetched
        tip = branches.get(pr.branch)
        if (
            tip
            and tip != pr.head
            and _git(repo, "cat-file", "-e", f"{pr.head}^{{commit}}", check=False).returncode
        ):
            _git(repo, "fetch", "-q", "origin", f"pull/{pr.number}/head", check=False)
    cwds = _cwds()
    if cwds is None:
        print(
            "! can't list processes' working directories; keeping every worktree", file=sys.stderr
        )
    return plan(
        worktrees,
        branches,
        prs,
        sorted(cache.glob("dev-venv*")),
        is_ancestor=lambda a, b: (
            a == b or not _git(repo, "merge-base", "--is-ancestor", a, b, check=False).returncode
        ),
        venv_name=venv_name,
        busy=lambda p: cwds is None or inside(p, cwds),
    )


def render(p: Plan, cache: Path) -> str:
    lines = []
    if p.worktrees:
        w = max(len(wt.path.name) for wt, _ in p.worktrees)
        lines.append(f"Merged worktrees ({CLAUDE_WORKTREES}/):")
        lines += [f"  {wt.path.name:{w}}  {why:5}  {wt.branch}" for wt, why in p.worktrees]
        if p.worktree_venvs:
            lines.append(f"  … and their {len(p.worktree_venvs)} dev venv(s)")
    if p.branches:
        lines.append("Merged branches:")
        lines += [f"  {why:5}  {name}" for name, why in p.branches]
    if p.venvs:
        lines.append(f"Venvs no checkout uses ({cache}): {len(p.venvs)}")
    if p.kept:
        lines.append("Kept:")
        lines += [f"  {what}: {why}" for what, why in p.kept]
    return "\n".join(lines) or "Nothing to prune."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Tidy up after merged PRs: worktrees, branches, dev venvs."
    )
    ap.add_argument("--apply", action="store_true", help="remove them (default: only list)")
    args = ap.parse_args(argv)
    repo = Path(
        _git(Path.cwd(), "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    ).parent
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "foldyard"
    p = gather(repo, cache)
    print(render(p, cache))
    if not (p.worktrees or p.branches or p.venvs):
        return 0
    if not args.apply:
        print("\nA dry run — `just prune --apply` removes them.")
        return 0
    status = apply(p, repo)
    print("\n✓ pruned" if not status else "\n✗ pruned, with the refusals above")
    return status


if __name__ == "__main__":
    sys.exit(main())
