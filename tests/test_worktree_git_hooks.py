"""foldyard's own git on your computer runs none of the repo's hooks.

The hooks live in the shared ``.git`` — ``.git/hooks/*`` and, since git 2.54, ``[hook "<name>"]``
entries in ``.git/config`` — and every box can write both. A ``fy worktree add`` on the host once
ran a consumer's lefthook ``post-checkout`` hooks there, in the new checkout, before it was even
registered. These drive REAL git (no mock): what is under test is what git itself honours.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from foldyard import config, machine, stack, worktree


def _git(repo: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, **kw)


def _git_has_config_hooks() -> bool:
    """Does this git read ``[hook "<name>"]`` from config (git 2.54+)? ``git hook list`` came with
    them; an older git doesn't know the subcommand."""
    probe = subprocess.run(["git", "hook", "list", "-h"], capture_output=True, text=True)
    return "usage: git hook list" in probe.stdout + probe.stderr


def _hook_script(marker: Path, label: str) -> str:
    return f'#!/bin/sh\necho "{label} $*" >> "{marker}"\n'


@pytest.fixture
def hooked_repo(tmp_path):
    """A real main repo whose shared ``.git`` carries every kind of hook a box could plant:
    hookdir scripts for ``post-checkout`` and ``reference-transaction``, and config hooks — one
    with a name that a ``-c key=value`` argument could not address (``=``, dots, case)."""
    main, marker = tmp_path / "main", tmp_path / "marker"
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "t")
    (main / "f.txt").write_text("base\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "base")
    hooks = main / ".git" / "hooks"
    for event in ("post-checkout", "reference-transaction"):
        (hooks / event).write_text(_hook_script(marker, f"hookdir-{event}"))
        (hooks / event).chmod(0o755)
    for name in ("lint", "we=ird.Na me"):
        _git(main, "config", f"hook.{name}.event", "post-checkout")
        _git(main, "config", "--add", f"hook.{name}.event", "reference-transaction")
        _git(main, "config", f"hook.{name}.command", f'echo "config-{name}" >> "{marker}"')
    return {"main": main, "marker": marker}


def test_the_rig_hooks_do_fire_under_plain_git(hooked_repo, tmp_path):
    # The control: without it, "no marker" below would prove nothing about foldyard.
    main, marker = hooked_repo["main"], hooked_repo["marker"]
    assert (
        _git(main, "worktree", "add", "-q", "-b", "plain", str(tmp_path / "plain")).returncode == 0
    )
    fired = marker.read_text()
    assert "hookdir-post-checkout" in fired
    assert "hookdir-reference-transaction" in fired
    if _git_has_config_hooks():
        assert "config-lint" in fired
        assert "config-we=ird.Na me" in fired


def test_host_git_runs_no_hook_of_either_kind(hooked_repo, tmp_path):
    main, marker = hooked_repo["main"], hooked_repo["marker"]
    wt = tmp_path / "quiet"
    got = worktree._host_git(
        main, "worktree", "add", "-q", "-b", "quiet", str(wt), capture_output=True
    )
    assert got.returncode == 0, got.stderr
    assert (wt / "f.txt").read_text() == "base\n"  # the checkout itself still happened
    assert not marker.exists()


def test_host_git_keeps_the_callers_own_runtime_config(hooked_repo, tmp_path, monkeypatch):
    # The hooks are switched off through GIT_CONFIG_COUNT, which the operator may already be
    # using: theirs must survive, numbered ahead of ours.
    main = hooked_repo["main"]
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "fy.test")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "kept")
    got = worktree._host_git(main, "config", "fy.test", capture_output=True, text=True)
    assert got.stdout.strip() == "kept"
    assert not hooked_repo["marker"].exists()


def test_host_git_does_not_run_the_repos_fsmonitor(hooked_repo, tmp_path):
    # core.fsmonitor in the shared config names a command git runs on every index refresh —
    # `worktree remove`'s dirty check (`git status`) and the snapshot's `git add -A` among them.
    main = hooked_repo["main"]
    probe, seen = tmp_path / "fsmonitor.sh", tmp_path / "fsmonitor-ran"
    probe.write_text(f'#!/bin/sh\necho ran >> "{seen}"\nexit 1\n')
    probe.chmod(0o755)
    _git(main, "config", "core.fsmonitor", str(probe))
    assert _git(main, "status", "--porcelain").returncode == 0
    assert seen.exists()  # the control: plain git runs it
    seen.unlink()
    got = worktree._host_git(main, "status", "--porcelain", capture_output=True, text=True)
    assert got.returncode == 0
    assert not seen.exists()


def test_the_uncommitted_work_snapshot_fires_no_hook(hooked_repo, tmp_path):
    # Its `update-ref` is a ref transaction, and `git add -A` refreshes the index.
    main, marker = hooked_repo["main"], hooked_repo["marker"]
    wt = tmp_path / "wt"
    worktree._host_git(main, "worktree", "add", "-q", "-b", "feat", str(wt), capture_output=True)
    (wt / "new.txt").write_text("work\n")
    assert worktree._snapshot_uncommitted(wt, "feat")
    assert not marker.exists()


@pytest.fixture
def real_add(hooked_repo, tmp_path, monkeypatch):
    """``worktree.add`` against the real repo, with everything but git stubbed."""
    main = hooked_repo["main"]
    wt_root = tmp_path / "main-worktrees"
    monkeypatch.setattr(stack, "main_repo", lambda: main)
    monkeypatch.setattr(config, "worktrees_root", lambda m: wt_root)
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(config, "worktree_base", lambda: None)
    monkeypatch.setattr(config, "worktree_init", lambda: None)
    monkeypatch.setattr(machine, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(worktree, "_seed_keyless_posture", lambda name: None)
    monkeypatch.setattr(worktree, "_say_unmasked", lambda *a, **k: None)
    monkeypatch.delenv("WORKTREE", raising=False)
    yield {**hooked_repo, "wt_root": wt_root}
    os.environ.pop("WORKTREE", None)


def test_worktree_add_runs_none_of_the_repos_hooks(real_add, capsys):
    assert worktree.add("feat") == 0
    assert (real_add["wt_root"] / "feat" / "f.txt").read_text() == "base\n"
    assert not real_add["marker"].exists()
    out = capsys.readouterr().out
    # Reporting is part of the fix: skipping them silently would leave "my post-checkout setup
    # never ran" a mystery.
    assert "skipped the repo's git hooks (post-checkout, reference-transaction)" in out


def test_worktree_add_says_nothing_about_hooks_when_there_are_none(real_add, capsys):
    main = real_add["main"]
    for event in ("post-checkout", "reference-transaction"):
        (main / ".git" / "hooks" / event).unlink()
    for name in ("lint", "we=ird.Na me"):
        _git(main, "config", "--remove-section", f"hook.{name}")
    assert worktree.add("feat") == 0
    assert "skipped the repo's git hooks" not in capsys.readouterr().out


def test_a_hook_that_is_not_executable_is_not_reported(real_add, capsys):
    # git skips a hookdir file without the x bit (it only warns), so it wasn't "skipped" by us.
    main = real_add["main"]
    (main / ".git" / "hooks" / "reference-transaction").unlink()
    (main / ".git" / "hooks" / "post-checkout").chmod(0o644)
    for name in ("lint", "we=ird.Na me"):
        _git(main, "config", "--remove-section", f"hook.{name}")
    assert worktree.add("feat") == 0
    assert "skipped the repo's git hooks" not in capsys.readouterr().out


# ── config hooks behind an include the HOST's own git never takes ─────────────────────────
# `git worktree add`'s child gits run with GIT_DIR=<main>/.git/worktrees/<name> and on the new
# branch, and evaluate conditional includes THERE. A hook defined behind
# `[includeIf "gitdir:**/.git/worktrees/**"]` (or `onbranch:`) is invisible to `git -C main config`
# and so was never disabled by name — the children ran it (PR #148 review). Every include target is
# read whatever its condition, and a target foldyard can't read refuses the git it was for.

needs_config_hooks = pytest.mark.skipif(
    not _git_has_config_hooks(), reason="this git has no config hooks (git 2.54+)"
)


def _plant_hidden_hook(main: Path, marker: Path, condition: str, rel: str = "extra.cfg") -> Path:
    """A config hook in ``.git/<rel>``, included from ``.git/config`` only under ``condition``."""
    target = main / ".git" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f'[hook "hidden"]\n\tevent = reference-transaction\n\tevent = post-checkout\n'
        f'\tcommand = echo hidden >> "{marker}"\n'
    )
    _git(main, "config", f"includeIf.{condition}.path", rel)
    return target


@needs_config_hooks
@pytest.mark.parametrize("condition", ["gitdir:**/.git/worktrees/**", "onbranch:wt/**"])
def test_a_hook_behind_a_condition_only_the_new_worktree_meets_does_not_run(
    real_add, condition, capsys
):
    main, marker = real_add["main"], real_add["marker"]
    for name in ("lint", "we=ird.Na me"):  # only the hidden one, so the marker is its alone
        _git(main, "config", "--remove-section", f"hook.{name}")
    _plant_hidden_hook(main, marker, condition)
    assert worktree.add("feat") == 0
    assert not marker.exists()
    assert "post-checkout" in capsys.readouterr().out  # reported, though main's git can't see it


@needs_config_hooks
def test_the_hidden_hook_does_fire_without_disabling_it_by_name(hooked_repo, tmp_path):
    # The control: hooksPath=/dev/null alone, and the include's condition met only by the child.
    main, marker = hooked_repo["main"], hooked_repo["marker"]
    for name in ("lint", "we=ird.Na me"):
        _git(main, "config", "--remove-section", f"hook.{name}")
    _plant_hidden_hook(main, marker, "gitdir:**/.git/worktrees/**")
    wt = tmp_path / "plain"
    _git(main, "-c", "core.hooksPath=/dev/null", "worktree", "add", "-q", "-b", "p", str(wt))
    assert "hidden" in marker.read_text()


@needs_config_hooks
def test_a_nested_include_is_followed_from_where_its_includer_lives(real_add):
    # Relative to the INCLUDING file's directory, as git resolves it — and nested.
    main, marker = real_add["main"], real_add["marker"]
    for name in ("lint", "we=ird.Na me"):
        _git(main, "config", "--remove-section", f"hook.{name}")
    sub = main / ".git" / "sub"
    sub.mkdir()
    (sub / "first.cfg").write_text(
        '[includeIf "gitdir:**/.git/worktrees/**"]\n\tpath = second.cfg\n'
    )
    _plant_hidden_hook(main, marker, "onbranch:nothing-matches", rel="sub/second.cfg")
    _git(main, "config", "--unset", "includeIf.onbranch:nothing-matches.path")
    _git(main, "config", "includeIf.gitdir:**/.git/worktrees/**.path", "sub/first.cfg")
    assert worktree.add("feat") == 0
    assert not marker.exists()


def test_an_include_cycle_ends(real_add):
    main = real_add["main"]
    (main / ".git" / "loop.cfg").write_text("[include]\n\tpath = loop.cfg\n")
    _git(main, "config", "includeIf.onbranch:nothing.path", "loop.cfg")
    hooks = worktree._config_hooks(main)  # returns, rather than recursing forever
    assert "lint" in hooks


def test_an_include_target_that_cannot_be_read_refuses_the_add(real_add, capsys):
    # A missing file is one the box can write the moment the check has passed: fail closed.
    main = real_add["main"]
    _git(main, "config", "includeIf.gitdir:**/.git/worktrees/**.path", "not-there.cfg")
    assert worktree.add("feat") != 0
    assert not (real_add["wt_root"] / "feat").exists()  # git never ran
    err = capsys.readouterr().err
    assert "not-there.cfg" in err and "hooks" in err


# The WALK needs no config-hook support (`git config --list` lists any `hook.*` key), so these run
# on every git — CI's 2.43 included — where the execution tests above are skipped.


@pytest.mark.parametrize("condition", ["gitdir:**/.git/worktrees/**", "onbranch:wt/**"])
def test_the_scan_finds_a_hook_behind_a_condition_main_does_not_meet(hooked_repo, condition):
    main = hooked_repo["main"]
    _plant_hidden_hook(main, hooked_repo["marker"], condition)
    assert "hidden" not in _git(main, "config", "--get-regexp", r"^hook\.").stdout  # main can't
    assert worktree._config_hooks(main)["hidden"] == {"reference-transaction", "post-checkout"}


def test_the_scan_follows_a_nested_include_from_its_includers_directory(hooked_repo):
    main = hooked_repo["main"]
    sub = main / ".git" / "sub"
    sub.mkdir()
    (sub / "first.cfg").write_text(
        '[includeIf "gitdir:**/.git/worktrees/**"]\n\tpath = second.cfg\n'
    )
    _plant_hidden_hook(main, hooked_repo["marker"], "onbranch:nothing-matches", "sub/second.cfg")
    _git(main, "config", "--unset", "includeIf.onbranch:nothing-matches.path")
    _git(main, "config", "includeIf.gitdir:**/.git/worktrees/**.path", "sub/first.cfg")
    assert "hidden" in worktree._config_hooks(main)


def test_a_git_too_old_to_name_scopes_refuses_with_the_version_it_needs(hooked_repo, monkeypatch):
    # `--show-scope` came in git 2.26: fail closed, but say what fixes it.
    real = subprocess.run

    def old_git(cmd, **kw):
        if "--show-scope" in cmd:
            return subprocess.CompletedProcess(cmd, 129, "", "error: unknown option `show-scope'\n")
        return real(cmd, **kw)

    monkeypatch.setattr(worktree.subprocess, "run", old_git)
    with pytest.raises(worktree.HookScanError, match=r"git 2\.26"):
        worktree._config_hooks(hooked_repo["main"])


def test_a_snapshot_it_cannot_take_says_so(hooked_repo, tmp_path, capsys):
    # Skipped because the worktree's config can't be checked for hooks: nobody should go looking
    # for a refs/fy/removed/… ref that was never written.
    main = hooked_repo["main"]
    wt = tmp_path / "wt"
    worktree._host_git(main, "worktree", "add", "-q", "-b", "feat", str(wt), capture_output=True)
    (wt / "new.txt").write_text("work\n")
    _git(main, "config", "include.path", "not-there.cfg")
    assert worktree._snapshot_uncommitted(wt, "feat") is None
    err = capsys.readouterr().err
    assert "NOT saved" in err and "not-there.cfg" in err
