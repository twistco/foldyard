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

import yard_stub
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


def test_parking_uncommitted_work_fires_no_hook(hooked_repo):
    # The host's half of the parking (the commit itself is the yard's): `update-ref` is a ref
    # transaction.
    main, marker = hooked_repo["main"], hooked_repo["marker"]
    tree = _git(main, "rev-parse", "HEAD^{tree}").stdout.strip()
    oid = _git(main, "commit-tree", tree, "-p", "HEAD", "-m", "parked").stdout.strip()
    ref = worktree._park(main, "feat", oid, worktree._hookless_env(main))
    assert ref and _git(main, "rev-parse", ref).stdout.strip() == oid
    assert not marker.exists()


@pytest.fixture
def real_add(hooked_repo, tmp_path, monkeypatch):
    """``worktree.add`` against the real repo, with everything but git stubbed — the yard's
    checkout included, which runs its git here (:mod:`yard_stub`'s ``local``)."""
    main = hooked_repo["main"]
    wt_root = tmp_path / "main-worktrees"
    monkeypatch.setattr(stack, "main_repo", lambda: main)
    monkeypatch.setattr(config, "worktrees_root", lambda m: wt_root)
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(config, "worktree_base", lambda: None)
    monkeypatch.setattr(config, "worktree_init", lambda: None)
    monkeypatch.setattr(machine, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(worktree, "_say_unmasked", lambda *a, **k: None)
    monkeypatch.delenv("WORKTREE", raising=False)
    yard_stub.install(monkeypatch, mode="local")
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


def test_a_registered_worktrees_branch_is_never_asked_of_the_git_dir_its_git_file_names(
    real_add, tmp_path, monkeypatch, capsys, fifos
):
    # Registered on your computer, but its `.git` file is the box's to rewrite: pointed at a git
    # dir the box built, git run in the checkout reads that dir's HEAD and config — its branch
    # name, and an include on a FIFO, which blocks every git that reads it. `list` and the TUI's
    # card read the branch from main's `.git/worktrees/<name>` instead, and nothing runs there.
    from foldyard import devmode

    monkeypatch.setattr(devmode, "main_repo", lambda: real_add["main"])
    assert worktree.add("feat") == 0
    wt = real_add["wt_root"] / "feat"

    def shown() -> tuple[str, str, str]:
        capsys.readouterr()
        assert worktree.list_() == 0
        line = next(ln for ln in capsys.readouterr().out.splitlines() if " feat " in ln)
        card: dict = {"path": str(wt)}
        devmode._describe_checkout(card, "feat")
        main_card: dict = {"path": str(real_add["main"])}
        devmode._describe_checkout(main_card, "")
        return line.split()[1], card["branch"], main_card["branch"]

    # As git says it, while the `.git` file is git's.
    assert shown() == ("wt/feat", "wt/feat", "main")

    planted = tmp_path / "planted"
    subprocess.run(["git", "init", "-q", "-b", "planted", str(planted)], check=True)
    _git(planted, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty",
         "-m", "x")  # fmt: skip
    _git(planted, "config", "include.path", str(fifos.plant(tmp_path / "planted.cfg")))
    (wt / ".git").write_text(f"gitdir: {planted / '.git'}\n")

    returned, got = fifos.returns_within(30, shown)
    assert returned, "a git read the box's FIFO"
    listed, card, main = got
    assert "planted" not in (listed, card) and card == "" and main == "main"
    assert not real_add["marker"].exists()


@pytest.mark.parametrize("fifo", ["gitdir", "config-include"])
def test_a_fifo_in_mains_git_cannot_hang_the_branch_read(
    real_add, monkeypatch, capsys, fifos, fifo
):
    # Main's `.git/worktrees/<id>` is the box's to write too. With a reftable placeholder for HEAD
    # the branch comes from main's `git worktree list`, and that git blocks on a FIFO in its
    # records (the worktree's `gitdir`) or in main's config (an include). `fy tui` asks on its
    # event loop every 5 s, and `fy worktree list` once: each is bounded, and reads as no branch.
    from foldyard import devmode

    main = real_add["main"]
    monkeypatch.setattr(devmode, "main_repo", lambda: main)
    monkeypatch.setattr(worktree, "_LISTING_TIMEOUT", 1)
    assert worktree.add("feat") == 0
    wt = real_add["wt_root"] / "feat"
    admin = main / ".git" / "worktrees" / "feat"
    (admin / "HEAD").write_text("ref: refs/heads/.invalid\n")
    if fifo == "gitdir":
        fifos.plant(admin / "gitdir")
    else:
        _git(main, "config", "include.path", str(fifos.plant(main / ".git" / "planted.cfg")))
    capsys.readouterr()

    returned, got = fifos.returns_within(10, lambda: worktree._branch_shown(main, wt, timeout=1))
    assert returned and got == ""
    if fifo == "gitdir":  # one in main's config blocks every git in main: `fy` finds main by one
        returned, _ = fifos.returns_within(10, worktree.list_)
        assert returned


def test_mains_branch_read_is_bounded_on_a_git_that_scans_the_config_first(
    hooked_repo, monkeypatch, fifos, capsys
):
    # On a git older than 2.44, `_git_read` scans every config file for a promisor remote before
    # it runs — `git config`, which blocks on a FIFO include like any git. The TUI's main card asks
    # with a timeout, and the scan keeps to it: refused, said, and read as no branch.
    main = hooked_repo["main"]
    monkeypatch.setattr(worktree, "_git_can_refuse_lazy_fetch", lambda: False)
    _git(main, "config", "include.path", str(fifos.plant(main / ".git" / "planted.cfg")))
    returned, got = fifos.returns_within(10, lambda: worktree._branch_of(main, timeout=1))
    assert returned and got == ""
    err = capsys.readouterr().err
    # Said as what it is — git didn't answer in time — not as a config it couldn't check for hooks
    # (on a busy machine there is nothing to fix in it).
    assert "git didn't answer within 1s" in err
    assert "hooks" not in err and "Fix or remove that include" not in err


def test_host_git_gives_up_on_a_fifo_in_mains_records(hooked_repo, tmp_path, fifos, capsys):
    # `git worktree remove` reads the worktree's record in main's `.git/worktrees`, which the box
    # can make a FIFO: bounded, a failed run, said.
    main, wt = hooked_repo["main"], tmp_path / "on"
    assert _git(main, "worktree", "add", "-q", "-b", "on", str(wt)).returncode == 0
    fifos.plant(main / ".git" / "worktrees" / "on" / "gitdir")
    returned, got = fifos.returns_within(
        10, lambda: worktree._host_git(main, "worktree", "remove", "--force", str(wt), timeout=1)
    )
    assert returned and got.returncode != 0
    assert "git didn't answer within 1s" in capsys.readouterr().err


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


def test_parking_refuses_a_config_it_cannot_check_for_hooks(hooked_repo):
    # The ref write is a ref transaction: with an include it can't read, no git runs and no ref is
    # claimed (`remove` refuses before the yard is even asked — tests/test_worktree_remove.py).
    main = hooked_repo["main"]
    _git(main, "config", "include.path", "not-there.cfg")
    with pytest.raises(worktree.HookScanError, match=r"not-there\.cfg"):
        worktree._hookless_env(main)


@pytest.mark.parametrize("ref_format", ["files", "reftable"])
def test_a_linked_worktrees_branch_reads_as_git_says_it(tmp_path, ref_format):
    # On your computer a linked worktree's branch is read from main's `.git/worktrees`, never asked
    # of git in it: what it reads must be what `git rev-parse --abbrev-ref HEAD` said there — on a
    # branch and detached, and in a reftable repo (git 2.45+), whose admin HEAD is a placeholder.
    main = tmp_path / "main"
    made = subprocess.run(
        ["git", "init", "-q", f"--ref-format={ref_format}", "-b", "main", str(main)],
        capture_output=True,
    )
    if made.returncode != 0:
        pytest.skip(f"this git can't make a {ref_format} repo")
    _git(main, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty",
         "-m", "x")  # fmt: skip
    _git(main, "worktree", "add", "-q", "-b", "wt/on", str(tmp_path / "on"))
    _git(main, "worktree", "add", "-q", "--detach", str(tmp_path / "off"))
    for wt, said in ((tmp_path / "on", "wt/on"), (tmp_path / "off", "HEAD")):
        assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == said
        assert worktree._branch_read(main, wt) == said


def test_a_relative_git_file_reads_through_a_symlinked_worktrees_root(tmp_path):
    # `worktree.useRelativePaths` (git 2.48+) writes the `.git` file relative to the checkout's
    # REAL directory. A worktrees root named through a symlink (`[machine].worktrees_root`,
    # `FOLDYARD_WORKTREES_ROOT`) at another depth must still find main's record, as git does.
    main = tmp_path / "main"
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    _git(main, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty",
         "-m", "x")  # fmt: skip
    real_root = tmp_path / "deep" / "er" / "trees"
    real_root.mkdir(parents=True)
    linked_root = tmp_path / "trees"
    linked_root.symlink_to(real_root)
    _git(main, "-c", "worktree.useRelativePaths=true", "worktree", "add", "-q", "-b", "wt/on",
         str(real_root / "on"))  # fmt: skip
    if (real_root / "on" / ".git").read_text().strip().startswith("gitdir: /"):
        pytest.skip("this git writes no relative worktree paths (git 2.48+)")
    assert _git(linked_root / "on", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "wt/on"
    assert worktree._branch_read(main, linked_root / "on") == "wt/on"
