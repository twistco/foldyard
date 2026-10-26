"""``just prune`` (tests/tools/prune.py): the Claude Code worktrees, local branches and dev venvs
a merged PR leaves behind.

The planner is pure and tested over plain data; ``apply`` runs against a scratch git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools import prune
from tools.prune import PR, Worktree

MAIN = Path("/src/foldyard")
CLAUDE = MAIN / ".claude" / "worktrees"

# tips and PR heads: a linear history main_old → main_tip → pr_head → after_merge, where only
# the first two are on origin/main (a squash merge puts none of the PR's own commits there)
HISTORY = ["main_old", "main_tip", "pr_head", "after_merge"]
ON_MAIN = {"main_old", "main_tip"}


def _is_ancestor(a: str, b: str) -> bool:
    if b == prune.MAIN_REF:
        return a in ON_MAIN
    return a in HISTORY and b in HISTORY and HISTORY.index(a) <= HISTORY.index(b)


def _venv(path: Path) -> str:
    return f"dev-venv-{path.name}"


def _plan(worktrees, branches, prs=(), venvs=(), busy=()):
    return prune.plan(
        worktrees=[Worktree(MAIN, "workshop"), *worktrees],
        branches={"main": "main_tip", "workshop": "after_merge", **branches},
        prs=list(prs),
        venvs=[Path("/cache") / v for v in venvs],
        is_ancestor=_is_ancestor,
        venv_name=_venv,
        busy=lambda p: p in busy,
    )


# ── merged_by ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tip", "prs", "want"),
    [
        ("pr_head", [PR(7, "feat", "pr_head")], "#7"),
        # behind its PR's head: the review commits were pushed from elsewhere
        ("main_tip", [PR(7, "feat", "pr_head")], "#7"),
        # a commit made after the merge is work the PR doesn't have
        ("after_merge", [PR(7, "feat", "pr_head")], None),
        # a PR from another branch says nothing about this one
        ("pr_head", [PR(7, "other", "pr_head")], None),
        ("main_old", [], "main"),
        ("pr_head", [], None),
    ],
)
def test_merged_by_names_what_holds_every_commit_on_the_branch(tip, prs, want):
    assert prune.merged_by("feat", tip, prs, _is_ancestor) == want


def test_on_main_alone_is_not_merged_when_a_pr_is_required():
    # a fresh branch sits on main too: only a merged PR shows work was done and shipped
    assert prune.merged_by("feat", "main_tip", [], _is_ancestor, require_pr=True) is None


# ── plan: worktrees ───────────────────────────────────────────────────────────────────────────


def test_a_merged_clean_claude_worktree_goes_with_its_branch_and_venv():
    wt = Worktree(CLAUDE / "feat", "feat")
    p = _plan([wt], {"feat": "pr_head"}, [PR(7, "feat", "pr_head")], ["dev-venv-feat"])
    assert p.worktrees == [(wt, "#7")]
    assert p.branches == [("feat", "#7")]
    assert p.venvs == [Path("/cache/dev-venv-feat")]


@pytest.mark.parametrize(
    ("wt", "busy", "why"),
    [
        (Worktree(CLAUDE / "feat", "feat", dirty=True), (), "uncommitted"),
        (Worktree(CLAUDE / "feat", "feat", locked=True), (), "locked"),
        (Worktree(CLAUDE / "feat", "feat"), (CLAUDE / "feat",), "in use"),
    ],
)
def test_a_merged_worktree_stays_when_removing_it_could_lose_something(wt, busy, why):
    p = _plan([wt], {"feat": "pr_head"}, [PR(7, "feat", "pr_head")], ["dev-venv-feat"], busy)
    assert p.worktrees == []
    assert p.branches == []  # still checked out
    assert p.venvs == []
    assert [k for k, _ in p.kept] == [str(wt.path)]
    assert why in p.kept[0][1]


def test_a_worktree_outside_claude_worktrees_is_never_removed():
    # `fy worktree add` checkouts have containers and a registry entry `fy worktree remove` clears
    wt = Worktree(Path("/src/foldyard-worktrees/feat"), "feat")
    p = _plan([wt], {"feat": "pr_head"}, [PR(7, "feat", "pr_head")], ["dev-venv-feat"])
    assert (p.worktrees, p.branches, p.venvs) == ([], [], [])


def test_a_fresh_worktree_on_main_stays():
    wt = Worktree(CLAUDE / "new-work", "new-work")
    p = _plan([wt], {"new-work": "main_tip"}, venvs=["dev-venv-new-work"])
    assert (p.worktrees, p.branches, p.venvs) == ([], [], [])


def test_a_detached_worktree_stays():
    wt = Worktree(CLAUDE / "agent", None)
    assert _plan([wt], {}, venvs=["dev-venv-agent"]).worktrees == []


# ── plan: branches ────────────────────────────────────────────────────────────────────────────


def test_a_branch_checked_out_nowhere_goes_once_main_holds_its_tip():
    p = _plan([], {"worktree-agent-1": "main_old", "unshipped": "pr_head"})
    assert p.branches == [("worktree-agent-1", "main")]


def test_main_and_the_main_checkouts_branch_are_never_deleted():
    p = _plan([], {}, [PR(3, "workshop", "after_merge"), PR(1, "main", "main_tip")])
    assert p.branches == []


# ── plan: venvs ───────────────────────────────────────────────────────────────────────────────


def test_every_venv_no_remaining_checkout_names_goes_including_the_old_shared_one():
    kept = Worktree(CLAUDE / "open-pr", "open-pr")
    p = _plan(
        [kept],
        {"open-pr": "after_merge"},
        venvs=["dev-venv-foldyard", "dev-venv-open-pr", "dev-venv-gone", "dev-venv"],
    )
    assert p.venvs == [Path("/cache/dev-venv-gone"), Path("/cache/dev-venv")]


def test_venv_name_matches_the_justfiles_cksum():
    # justfile: dev-venv-$(echo "{{_dir}}" | cksum | cut -d' ' -f1); pinned from the real cksum
    assert prune.venv_name(Path("/src/foldyard")) == "dev-venv-3394839273"


# ── parsing ───────────────────────────────────────────────────────────────────────────────────


def test_worktree_list_porcelain_parses_branch_detached_and_locked():
    text = (
        "worktree /src/foldyard\nHEAD aaa\nbranch refs/heads/main\n\n"
        "worktree /src/foldyard/.claude/worktrees/a\nHEAD bbb\ndetached\n\n"
        "worktree /src/foldyard/.claude/worktrees/b\nHEAD ccc\nbranch refs/heads/feat/b\n"
        "locked claude agent\n\n"
    )
    assert prune.parse_worktrees(text) == [
        Worktree(Path("/src/foldyard"), "main"),
        Worktree(Path("/src/foldyard/.claude/worktrees/a"), None),
        Worktree(Path("/src/foldyard/.claude/worktrees/b"), "feat/b", locked=True),
    ]


def test_lsof_cwds_and_busy_match_whole_path_components():
    cwds = prune.parse_lsof_cwds("p1\nfcwd\nn/src/wt/a/sub\np2\nfcwd\nn/home\n")
    assert prune.inside(Path("/src/wt/a"), cwds)
    assert not prune.inside(Path("/src/wt/a-b"), prune.parse_lsof_cwds("p1\nfcwd\nn/src/wt/a\n"))
    assert not prune.inside(Path("/src/wt/b"), cwds)


# ── apply, against a real repo ────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_apply_removes_the_worktree_then_the_branch_then_the_venv(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    wt_path = repo / ".claude" / "worktrees" / "feat"
    _git(repo, "worktree", "add", "-q", "-b", "feat", str(wt_path))
    venv = tmp_path / "cache" / "dev-venv-1"
    venv.mkdir(parents=True)
    plan = prune.Plan(
        worktrees=[(Worktree(wt_path, "feat"), "#7")], branches=[("feat", "#7")], venvs=[venv]
    )

    assert prune.apply(plan, repo) == 0

    assert not wt_path.exists()
    assert _git(repo, "branch", "--list", "feat") == ""
    assert not venv.exists()


def test_apply_keeps_the_branch_when_git_refuses_its_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    wt_path = repo / ".claude" / "worktrees" / "feat"
    _git(repo, "worktree", "add", "-q", "-b", "feat", str(wt_path))
    (wt_path / "new.txt").write_text("written after the plan was made\n")
    plan = prune.Plan(worktrees=[(Worktree(wt_path, "feat"), "#7")], branches=[("feat", "#7")])

    assert prune.apply(plan, repo) == 1

    assert (wt_path / "new.txt").exists()
    assert _git(repo, "branch", "--list", "feat") != ""
