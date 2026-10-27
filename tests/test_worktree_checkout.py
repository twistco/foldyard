"""`fy worktree add` checks the new worktree's files out in a container, never on your computer.

The shared ``.git`` every box can write can name FILTER DRIVERS (``filter.<d>.smudge`` /
``clean`` / ``process``, applied through ``.gitattributes`` or ``.git/info/attributes``), and git
runs them while it checks files out — so a host-side checkout was host code execution for anything
that can write the checkout, with no race needed. The host now does only the metadata step
(``git worktree add --no-checkout``: the branch, HEAD, the worktree's admin dir); the files and the
index come from git in the yard. These drive REAL git (no mock): what is under test is what git
itself runs. The engine is stubbed — either recorded only, or (``yard="local"``) by running the
container's git argv here, which is the closest a hermetic test gets to the container doing it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import yard_stub
from foldyard import config, machine, stack, worktree, worktree_registry

_real_run = subprocess.run
# Identity filters out of shell builtins (the suite's PATH holds no `cat`): each records that it ran.
_PASS = 'while IFS= read -r l || [ -n "$l" ]; do printf "%s\\n" "$l"; done'


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return _real_run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _oid(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


@pytest.fixture
def filtered_repo(tmp_path):
    """A real main repo whose shared ``.git`` names every kind of command a checkout can run:
    smudge/clean drivers from ``.git/config``, one only reachable through an ``includeIf`` that a
    child git in the new worktree meets (``gitdir:…/worktrees/…``), a ``process`` driver, an
    attribute only ``.git/info/attributes`` applies, an ``onbranch:``-included config hook, the
    hooks directory and ``core.fsmonitor``. Every one appends to ``marker``."""
    main, marker = tmp_path / "main", tmp_path / "marker"
    _real_run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "t")
    (main / ".gitattributes").write_text("x filter=x\n*.bin filter=other\ny filter=inc\n")
    (main / "x").write_text("hello\n")
    (main / "a.bin").write_text("data\n")
    (main / "y").write_text("why\n")
    (main / "w").write_text("info-attrs\n")
    (main / "zz-proc").write_text("last, so the control's failing process driver runs last\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "base")
    _git(main, "branch", "existing")
    _git(main, "remote", "add", "origin", str(tmp_path / "nowhere.git"))  # never fetched
    _git(main, "update-ref", "refs/remotes/origin/remote", "HEAD")
    for d in ("x", "other", "info"):
        _git(main, "config", f"filter.{d}.smudge", f'echo "smudge-{d}" >> "{marker}"; {_PASS}')
        _git(main, "config", f"filter.{d}.clean", f'echo "clean-{d}" >> "{marker}"; {_PASS}')
    _git(main, "config", "filter.proc.process", f"sh -c 'echo process >> \"{marker}\"; exit 1'")
    (main / ".git" / "info" / "attributes").write_text("w filter=info\nzz-proc filter=proc\n")
    scoped = tmp_path / "scoped.cfg"
    for stage in ("smudge", "clean"):  # written by git, which quotes them as its parser expects
        cmd = f'echo "{stage}-inc" >> "{marker}"; {_PASS}'
        _git(main, "config", "-f", str(scoped), f"filter.inc.{stage}", cmd)
    _git(main, "config", "includeIf.gitdir:**/.git/worktrees/**.path", str(scoped))
    onbranch = tmp_path / "onbranch.cfg"
    for event in ("reference-transaction", "post-checkout"):
        _git(main, "config", "-f", str(onbranch), "--add", "hook.ob.event", event)
    _git(
        main, "config", "-f", str(onbranch), "hook.ob.command", f'echo hook-onbranch >> "{marker}"'
    )
    _git(main, "config", "includeIf.onbranch:**.path", str(onbranch))
    _git(main, "config", "core.fsmonitor", f'echo fsmonitor >> "{marker}"; exit 1 #')
    for event in ("post-checkout", "reference-transaction", "post-index-change"):
        hook = main / ".git" / "hooks" / event
        hook.write_text(f'#!/bin/sh\necho "hookdir-{event}" >> "{marker}"\n')
        hook.chmod(0o755)
    return {"main": main, "marker": marker}


def test_the_rig_does_run_code_under_a_plain_git_worktree_add(filtered_repo, tmp_path):
    # The control: without it, "no marker" below would prove nothing about foldyard.
    main, marker = filtered_repo["main"], filtered_repo["marker"]
    _git(main, "worktree", "add", "-q", "-b", "plain", str(tmp_path / "plain"))
    ran = marker.read_text().split()
    for what in ("smudge-x", "smudge-other", "smudge-inc", "smudge-info", "process"):
        assert what in ran
    assert "hook-onbranch" in ran


# The three argv shapes `add` builds: a new branch (off the default), an existing local branch, and
# a branch only the remote has (`-b <branch> … origin/<branch>`).
SHAPES = {
    "new": ("wt/feat", ""),
    "existing": ("existing", ""),
    "remote": ("remote", ""),
}


@pytest.fixture
def real_add(filtered_repo, tmp_path, monkeypatch):
    """``worktree.add`` against the real repo, with the machine and the yard stubbed
    (:mod:`yard_stub`: recorded only, unless a test sets ``yard["mode"]``)."""
    main = filtered_repo["main"]
    wt_root = tmp_path / "main-worktrees"
    monkeypatch.setattr(stack, "main_repo", lambda: main)
    monkeypatch.setattr(config, "worktrees_root", lambda m: wt_root)
    monkeypatch.setattr(config, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(config, "worktree_base", lambda: None)
    monkeypatch.setattr(config, "worktree_init", lambda: None)
    monkeypatch.setattr(machine, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(worktree, "_seed_keyless_posture", lambda name: None)
    monkeypatch.setattr(worktree, "_say_unmasked", lambda *a, **k: None)
    monkeypatch.delenv("WORKTREE", raising=False)
    yard = yard_stub.install(monkeypatch, mode="none")
    yield {**filtered_repo, "wt_root": wt_root, "runs": yard["runs"], "yard": yard}
    os.environ.pop("WORKTREE", None)


def _add(shape: str) -> int:
    branch, base = SHAPES[shape]
    return worktree.add("feat", "" if shape == "new" else branch, base)


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_add_runs_nothing_the_shared_git_names_on_your_computer(real_add, shape):
    assert _add(shape) == 0
    assert not real_add["marker"].exists()  # no filter, hook or fsmonitor ran on the host
    # What the yard was handed: the metadata alone. The checkout holds only its `.git` file, and
    # git's admin dir for it a HEAD but no index — the files and the index are the yard's to write.
    seen = real_add["yard"]["seen"]
    assert seen["worktree"] == [".git"]
    assert "HEAD" in seen["admin"] and "index" not in seen["admin"]


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_yard_is_handed_the_commit_the_host_resolved(real_add, shape):
    # The VM can read a ref your computer just replaced as older or absent (ADR-0021), so the
    # container is never asked to resolve one: it gets the commit, read here.
    assert _add(shape) == 0
    branch = SHAPES[shape][0]
    (run,) = real_add["runs"]
    assert run[-1] == _oid(real_add["main"], f"refs/heads/{branch}")
    assert run[run.index("-w") + 1] == str(real_add["wt_root"] / "feat")


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_yard_checkout_leaves_a_worktree_git_sees_as_clean(real_add, shape):
    real_add["yard"]["mode"] = "local"
    # The rig's `process` driver fails by design (the control shows it would run); a yard whose
    # filters fail fails the checkout — that path is test_a_failed_checkout_keeps_nothing's.
    (real_add["main"] / ".git" / "info" / "attributes").write_text("w filter=info\n")
    assert _add(shape) == 0
    main, wt = real_add["main"], real_add["wt_root"] / "feat"
    # The filters ran — in the yard, which is what makes the files right.
    assert "smudge-x" in real_add["marker"].read_text()
    for f in ("x", "a.bin", "y", "w"):
        assert (wt / f).read_text() == (main / f).read_text()
    assert _oid(wt, "HEAD") == _oid(main, f"refs/heads/{SHAPES[shape][0]}")
    assert _git(wt, "-c", "core.fsmonitor=", "status", "--porcelain").stdout == ""
    assert worktree_registry.checkouts(main) == {"feat": wt.resolve()}


def test_the_remote_shape_still_tracks_the_remote_branch(real_add):
    # `-b <branch> … origin/<branch>` records its upstream — on your computer, which is where
    # .git/config is written (a VM write of it once cost a consumer theirs: ADR-0021, issue #6).
    assert _add("remote") == 0
    assert _git(real_add["main"], "config", "branch.remote.merge").stdout.strip() == (
        "refs/heads/remote"
    )


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_failed_checkout_keeps_nothing(real_add, shape, capsys):
    real_add["yard"]["mode"] = "partial"
    main, wt = real_add["main"], real_add["wt_root"] / "feat"
    before = _oid(main, "refs/heads/existing")
    assert _add(shape) == 1
    assert not wt.exists()
    assert str(wt) not in _git(main, "worktree", "list", "--porcelain").stdout
    assert worktree_registry.checkouts(main) == {}
    assert not real_add["marker"].exists()  # the undo ran no filter or fsmonitor either
    has_branch = _git(main, "show-ref", "--verify", "--quiet", f"refs/heads/{SHAPES[shape][0]}")
    # A branch `add` created is dropped with it, so a re-run starts the same way; one that was
    # already there is never touched.
    assert (has_branch.returncode == 0) == (shape == "existing")
    assert _oid(main, "refs/heads/existing") == before
    err = capsys.readouterr().err
    assert "fy worktree add feat" in err


def test_a_checkout_that_never_reached_your_computer_keeps_nothing(real_add, capsys):
    # Docker's engine makes a missing bind source: a VM that doesn't mount the worktrees root
    # would take the checkout onto its own disk and report success.
    real_add["yard"]["index"] = False
    assert _add("new") == 1
    assert not (real_add["wt_root"] / "feat").exists()
    assert worktree_registry.checkouts(real_add["main"]) == {}
    err = capsys.readouterr().err
    assert "never reached" in err and "fy machine recreate" in err


def test_a_fifo_for_the_git_file_does_not_hang_the_check(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    os.mkfifo(wt / ".git")
    assert worktree._index_on_host(wt) is False


def test_no_yard_refuses_before_git_writes_anything(real_add, capsys):
    real_add["yard"]["image"] = False
    main = real_add["main"]
    assert worktree.add("feat", base="existing") == 1
    assert not (real_add["wt_root"] / "feat").exists()
    assert not (main / ".git" / "worktrees").exists()
    assert _git(main, "show-ref", "--verify", "--quiet", "refs/heads/wt/feat").returncode == 1
    assert real_add["runs"] == []
    err = capsys.readouterr().err
    # Explicit, with what to run first — and never a quiet fall back to a checkout here.
    assert "proj-devbox:latest" in err
    assert "fy box build" in err and "fy worktree add feat --from existing" in err


def test_an_image_without_git_is_named(real_add, capsys):
    real_add["yard"]["rc"] = 127
    assert _add("new") == 1
    err = capsys.readouterr().err
    assert "no git" in err and "[box].image" in err


def test_lfs_files_are_said_to_be_pointers(real_add, capsys):
    main = real_add["main"]
    (main / ".gitattributes").write_text("*.psd filter=lfs diff=lfs merge=lfs -text\n")
    (main / ".git" / "info" / "attributes").write_text("")  # `commit -a` hashes: no failing driver
    _git(main, "-c", "core.hooksPath=/dev/null", "commit", "-qam", "lfs")
    real_add["marker"].unlink(missing_ok=True)
    assert _add("new") == 0
    assert "git lfs pull" in capsys.readouterr().out


def test_no_lfs_note_without_lfs(real_add, capsys):
    assert _add("new") == 0
    assert "git lfs" not in capsys.readouterr().out
