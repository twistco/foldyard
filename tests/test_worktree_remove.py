"""`fy worktree remove` reads the worktree's files in a container, never on your computer.

The shared ``.git`` every box can write can name a ``clean`` filter driver (``filter.<d>.clean``,
applied through ``.gitattributes`` or ``.git/info/attributes``), and git runs it for every file it
HASHES. Two steps of a removal hashed the checkout on your computer: ``git worktree remove``'s own
check for uncommitted work (a ``git status``, which re-hashes each file whose stat data has changed
— on a worktree checked out in the VM, every one), and the snapshot that parks that work on
``refs/fy/removed/…`` (``add -A``). Both run in the yard now; the host only writes the ref, after
checking the id it was handed names a commit, and deletes the checkout with ``--force``.

These drive REAL git (no mock): what is under test is what git itself runs. The yard is
:mod:`yard_stub` — answered (``none``), or run here (``local``: the container's script under this
``sh``, with ``FY_YARD=1`` set, so the rig's filter records WHERE it ran).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import yard_stub
from foldyard import box, config, machine, stack, transcripts, worktree, worktree_registry

_real_run = subprocess.run
# An identity filter out of shell builtins (the suite's PATH holds no `cat`).
_PASS = 'while IFS= read -r l || [ -n "$l" ]; do printf "%s\\n" "$l"; done'
NAME = "feat"
OLD = (946684800, 946684800)  # 2000-01-01: a stat change that makes git re-hash the file


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return _real_run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _refs(main: Path) -> list[str]:
    out = _git(main, "for-each-ref", "--format=%(refname)", "refs/fy/removed/").stdout
    return out.split()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A real main repo and a real linked worktree of it, registered, whose shared config names a
    ``clean`` filter for ``x`` that appends ``clean-x <where>`` to ``marker`` — ``yard`` when the
    yard stub ran it, ``host`` otherwise. Every file's mtime is moved, so any ``git status`` on the
    worktree re-hashes it. Everything but git and the yard is stubbed."""
    main, marker = tmp_path / "main", tmp_path / "marker"
    wt_root = tmp_path / "main-worktrees"
    wt = wt_root / NAME
    _real_run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "t")
    (main / ".gitattributes").write_text("x filter=x\n")
    (main / "x").write_text("hello\n")
    (main / "y").write_text("plain\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "base")
    assert _git(main, "worktree", "add", "-q", "-b", f"wt/{NAME}", str(wt)).returncode == 0
    where = 'w=host; [ -n "$FY_YARD" ] && w=yard'
    _git(main, "config", "filter.x.clean", f'{where}; echo "clean-x $w" >> "{marker}"; {_PASS}')
    for f in ("x", "y", ".gitattributes"):
        os.utime(wt / f, OLD)

    monkeypatch.setattr(stack, "main_repo", lambda: main)
    from foldyard import devmode

    monkeypatch.setattr(devmode, "main_repo", lambda: main)
    monkeypatch.setattr(config, "worktrees_root", lambda m: wt_root)
    monkeypatch.setattr(config, "project_prefix", lambda: "proj")
    monkeypatch.setattr(config, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(machine, "ensure", lambda *a, **k: None)
    teardown: list[str] = []
    monkeypatch.setattr(box, "main", lambda cmd, *a, **k: teardown.append(f"box:{cmd}") or 0)
    monkeypatch.setattr(stack, "nuke", lambda *a, **k: teardown.append("nuke") or 0)
    monkeypatch.setattr(stack, "remove_devbox_volumes", lambda *a, **k: 0)
    monkeypatch.setattr(transcripts, "sync_current", lambda *a, **k: 0)
    monkeypatch.delenv("WORKTREE", raising=False)
    worktree_registry.register(main, NAME, wt)
    yard = yard_stub.install(monkeypatch, mode="none")
    yield {"main": main, "wt": wt, "marker": marker, "yard": yard, "teardown": teardown}
    os.environ.pop("WORKTREE", None)


def _ran(rig) -> list[str]:
    marker = rig["marker"]
    return marker.read_text().splitlines() if marker.exists() else []


def _gone(rig) -> bool:
    return (
        not rig["wt"].exists()
        and str(rig["wt"]) not in _git(rig["main"], "worktree", "list", "--porcelain").stdout
    )


def _dirty(rig) -> None:
    (rig["wt"] / "x").write_text("EDITED\n")
    (rig["wt"] / "new.txt").write_text("BRAND NEW\n")


def test_the_rig_runs_the_clean_filter_under_a_plain_git_worktree_remove(rig):
    # The control: without it, "no marker" below would prove nothing about foldyard.
    assert _git(rig["main"], "worktree", "remove", str(rig["wt"])).returncode == 0
    assert "clean-x host" in _ran(rig)


def test_a_clean_worktree_is_removed_with_no_hashing_git_on_your_computer(rig):
    assert worktree.remove(NAME, assume_yes=True) == 0
    assert _gone(rig)
    assert _ran(rig) == []
    (check,) = rig["yard"]["checks"]
    assert check[-2:] == ["check", f"fy: uncommitted work in worktree '{NAME}' at removal"]
    assert _refs(rig["main"]) == []


@pytest.mark.spawns("sh")
def test_the_check_hashes_in_the_yard(rig):
    rig["yard"]["mode"] = "local"
    assert worktree.remove(NAME, assume_yes=True) == 0
    assert _gone(rig)
    ran = _ran(rig)
    assert "clean-x yard" in ran and "clean-x host" not in ran
    assert _refs(rig["main"]) == []  # nothing uncommitted, nothing parked


@pytest.mark.spawns("sh")
def test_uncommitted_work_without_force_is_refused_and_kept(rig, capsys):
    rig["yard"]["mode"] = "local"
    _dirty(rig)
    assert worktree.remove(NAME, assume_yes=True) == 1
    assert (rig["wt"] / "x").read_text() == "EDITED\n"
    assert (rig["wt"] / "new.txt").read_text() == "BRAND NEW\n"
    assert worktree_registry.is_recorded(rig["main"], NAME)
    assert _refs(rig["main"]) == []
    assert "clean-x host" not in _ran(rig)
    err = capsys.readouterr().err
    assert "uncommitted" in err and "--force" in err


@pytest.mark.spawns("sh")
def test_an_untracked_file_alone_is_uncommitted_work(rig):
    rig["yard"]["mode"] = "local"
    (rig["wt"] / "new.txt").write_text("BRAND NEW\n")
    assert worktree.remove(NAME, assume_yes=True) == 1
    assert (rig["wt"] / "new.txt").exists()


@pytest.mark.spawns("sh")
def test_a_config_that_hides_untracked_files_does_not_hide_them_from_the_check(rig):
    # Git's own check honours status.showUntrackedFiles, which the box-writable config can set to
    # `no`: an untracked file then reads as committed, and `git worktree remove` deletes it.
    rig["yard"]["mode"] = "local"
    _git(rig["main"], "config", "status.showUntrackedFiles", "no")
    (rig["wt"] / "new.txt").write_text("BRAND NEW\n")
    assert worktree.remove(NAME, assume_yes=True) == 1
    assert (rig["wt"] / "new.txt").exists()


@pytest.mark.spawns("sh")
def test_force_parks_the_work_from_the_yard_and_hashes_nothing_here(rig, capsys):
    rig["yard"]["mode"] = "local"
    main = rig["main"]
    _dirty(rig)
    assert worktree.remove(NAME, force=True) == 0
    assert _gone(rig)
    (ref,) = _refs(main)
    assert ref.startswith(f"refs/fy/removed/{NAME}-")
    assert _git(main, "show", f"{ref}:x").stdout == "EDITED\n"
    assert _git(main, "show", f"{ref}:new.txt").stdout == "BRAND NEW\n"
    assert _git(main, "rev-parse", f"{ref}^").stdout == _git(main, "rev-parse", f"wt/{NAME}").stdout
    assert _git(main, "log", "-1", "--format=%an <%ae>", ref).stdout.strip() == (
        "foldyard <foldyard@localhost>"
    )
    ran = _ran(rig)
    assert "clean-x yard" in ran and "clean-x host" not in ran
    out = capsys.readouterr().out
    assert ref in out and "restore" in out
    # …and it restores: the work comes back into a checkout of the branch.
    restored = main.parent / "restored"
    assert _git(main, "worktree", "add", "-q", str(restored), f"wt/{NAME}").returncode == 0
    assert _git(restored, "-c", "filter.x.clean=", "checkout", ref, "--", ".").returncode == 0
    assert (restored / "new.txt").read_text() == "BRAND NEW\n"


def test_force_writes_the_ref_the_yard_names_and_hashes_nothing_here(rig):
    # The host's part alone: check the id names a commit (`cat-file`, which reads an object and
    # hashes nothing), write the ref, delete the checkout with --force (no `git status`).
    main = rig["main"]
    _dirty(rig)
    tree = _git(main, "rev-parse", "HEAD^{tree}").stdout.strip()
    oid = _git(main, "commit-tree", tree, "-p", "HEAD", "-m", "parked").stdout.strip()
    rig["yard"]["out"] = f"fy-saved {oid}\n"
    assert worktree.remove(NAME, force=True) == 0
    assert _gone(rig)
    (ref,) = _refs(main)
    assert _git(main, "rev-parse", ref).stdout.strip() == oid
    assert _ran(rig) == []
    (check,) = rig["yard"]["checks"]
    assert check[-2] == "save"


@pytest.mark.spawns("sh")
def test_a_sha256_repo_is_parked_too(tmp_path, rig, monkeypatch):
    main = tmp_path / "sha"
    wt = tmp_path / "main-worktrees" / "sha"
    _real_run(["git", "init", "-q", "--object-format=sha256", str(main)], check=True)
    (main / "f").write_text("base\n")
    _git(main, "add", "-A")
    _git(main, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "base")
    _git(main, "worktree", "add", "-q", "-b", "wt/sha", str(wt))
    monkeypatch.setattr(stack, "main_repo", lambda: main)
    worktree_registry.register(main, "sha", wt)
    (wt / "f").write_text("EDITED\n")
    rig["yard"]["mode"] = "local"
    assert worktree.remove("sha", force=True) == 0
    (ref,) = _refs(main)
    assert len(_git(main, "rev-parse", ref).stdout.strip()) == 64
    assert _git(main, "show", f"{ref}:f").stdout == "EDITED\n"


@pytest.mark.parametrize("force", [False, True])
def test_no_yard_refuses_before_anything_is_torn_down(rig, force, capsys):
    rig["yard"]["image"] = False
    _dirty(rig)
    assert worktree.remove(NAME, force=force, assume_yes=True) == 1
    assert rig["teardown"] == []  # its box and stack are still up
    assert (rig["wt"] / "x").read_text() == "EDITED\n"
    assert worktree_registry.is_recorded(rig["main"], NAME)
    assert rig["yard"]["checks"] == [] and _ran(rig) == []
    err = capsys.readouterr().err
    # Explicit, with what to run first, and the honest way out — never a quiet check here.
    assert "proj-devbox:latest" in err and "fy box build" in err
    assert f"fy worktree remove {NAME}" in err and "--discard" in err


def test_discard_removes_without_checking_or_saving(rig, capsys):
    rig["yard"]["image"] = False
    _dirty(rig)
    assert worktree.remove(NAME, assume_yes=True, discard=True) == 0
    assert _gone(rig)
    assert rig["yard"]["checks"] == [] and _ran(rig) == [] and _refs(rig["main"]) == []
    assert "--discard" in capsys.readouterr().out  # the plan says what it won't do


def _blob(main: Path) -> str:
    return _git(main, "rev-parse", "HEAD:y").stdout.strip()


@pytest.mark.parametrize(
    "out, rc",
    [
        ("", 0),
        ("fy-clean\nfy-clean\n", 0),
        ("fy-clean", 3),
        ("fy-dirty\n", 0),  # a check never asked to save says dirty; asked, it must save
        ("fy-unchanged\n", 0),  # no longer an answer: dirty always has something to park
        ("fy-saved deadbeef\n", 0),
        ("fy-saved " + "a" * 40 + "\n", 0),  # no such object
        ("fy-saved BLOB\n", 0),  # an object, not a commit
        ("fy-saved " + "a" * 40 + "; rm -rf /\n", 0),
        ("\x1b[2Jfy-clean\n", 0),
        ("fy-cle\xffan\n", 0),
    ],
)
def test_output_it_cannot_vouch_for_refuses_and_keeps_the_worktree(rig, out, rc, capsys):
    _dirty(rig)
    rig["yard"]["out"] = out.replace("BLOB", _blob(rig["main"]))
    rig["yard"]["rc"] = rc
    assert worktree.remove(NAME, force=True) == 1
    assert (rig["wt"] / "x").read_text() == "EDITED\n"
    assert worktree_registry.is_recorded(rig["main"], NAME)
    assert _refs(rig["main"]) == []
    err = capsys.readouterr().err
    assert "nothing removed" in err.lower() or "not removed" in err.lower()
    assert "--discard" in err


def test_a_worktree_with_submodules_is_refused_without_force(rig, capsys):
    # Git's own rule, which its --force (always passed here now) skips: the worktree's admin dir
    # holds its submodules' repos, and removing it deletes their unpushed commits.
    admin = rig["main"] / ".git" / "worktrees" / NAME
    (admin / "modules").mkdir()
    assert worktree.remove(NAME, assume_yes=True) == 1
    assert rig["wt"].exists()
    assert "submodules" in capsys.readouterr().err


def test_a_locked_worktree_stays_locked_under_force(rig):
    _git(rig["main"], "worktree", "lock", str(rig["wt"]))
    assert worktree.remove(NAME, force=True) != 0
    assert rig["wt"].exists()


def test_a_config_it_cannot_check_for_hooks_refuses_before_the_yard_runs(rig, capsys):
    _git(rig["main"], "config", "include.path", "not-there.cfg")
    assert worktree.remove(NAME, force=True) == 1
    assert rig["wt"].exists() and rig["yard"]["checks"] == []
    assert "not-there.cfg" in capsys.readouterr().err


def test_an_orphan_is_still_deleted_under_force_without_the_yard(rig, capsys):
    # Git no longer tracks it, so there's nothing git could check or save there: unchanged — the
    # dir is deleted and the stale metadata pruned, with no container and no hashing git.
    import shutil

    shutil.rmtree(rig["main"] / ".git" / "worktrees" / NAME)
    rig["yard"]["image"] = False
    assert worktree.remove(NAME, force=True) == 0
    assert not rig["wt"].exists()
    assert rig["yard"]["checks"] == [] and _ran(rig) == []


def test_the_check_container_is_the_checkouts_yard(tmp_path, monkeypatch):
    # Golden: the image's own shell + git, the two mounts the checkout gets, the box's uid, no
    # capabilities, no network, a fixed identity for the parking commit, and the name and message
    # passed as ARGUMENTS — never spliced into the script.
    main = tmp_path / "repo"
    wt_root = tmp_path / "repo-worktrees"
    monkeypatch.setattr(config, "worktrees_root", lambda m: wt_root)
    cmd = worktree._uncommitted_cmd("podman", "img:1", main, wt_root / "a b", "a b", save=True)
    assert cmd == [
        "podman",
        "run",
        "--rm",
        "--user",
        "0",
        "--cap-drop",
        "ALL",
        "--network",
        "none",
        "--security-opt",
        "label=disable",
        "-v",
        f"{main}:{main}",
        "-v",
        f"{wt_root}:{wt_root}",
        "-w",
        str(wt_root / "a b"),
        "-e",
        "GIT_AUTHOR_NAME=foldyard",
        "-e",
        "GIT_AUTHOR_EMAIL=foldyard@localhost",
        "-e",
        "GIT_COMMITTER_NAME=foldyard",
        "-e",
        "GIT_COMMITTER_EMAIL=foldyard@localhost",
        "--entrypoint",
        "sh",
        "img:1",
        "-c",
        worktree._UNCOMMITTED_SH,
        "fy-uncommitted",
        "save",
        "fy: uncommitted work in worktree 'a b' at removal",
    ]
    assert (
        worktree._uncommitted_cmd("podman", "img:1", main, wt_root / "a", "a", save=False)[-2]
        == "check"
    )


def test_the_check_script_parses():
    got = _real_run(["sh", "-n", "-c", worktree._UNCOMMITTED_SH], capture_output=True, text=True)
    assert got.returncode == 0, got.stderr


# ── the parking commit itself (the yard's script, run here) ───────────────────────────────────

_YARD = ("podman", {"DOCKER_HOST": "unix:///nonexistent/fy-test"}, yard_stub.IMAGE)


@pytest.mark.spawns("sh")
def test_the_parking_commit_does_not_disturb_what_it_parks(rig):
    # It runs on the way OUT of a checkout, so it must not be able to change what is removed: the
    # check and the tree both go through a throwaway index in the container, never the worktree's
    # own, so neither that index nor HEAD changes.
    rig["yard"]["mode"] = "local"
    wt = rig["wt"]
    _dirty(rig)
    index = rig["main"] / ".git" / "worktrees" / NAME / "index"
    before = (index.read_bytes(), _git(wt, "rev-parse", "HEAD").stdout)
    verdict = worktree._uncommitted_in_yard(rig["main"], wt, NAME, _YARD, save=True)
    assert verdict is not None and verdict[0] == "saved"
    assert (index.read_bytes(), _git(wt, "rev-parse", "HEAD").stdout) == before
    assert (wt / "x").read_text() == "EDITED\n"


@pytest.mark.spawns("sh")
def test_the_parking_commit_leaves_out_what_gitignore_ignores(rig):
    # What keeps it cheap (no node_modules) is also its blind spot — pinned so the tradeoff is a
    # decision rather than a surprise.
    rig["yard"]["mode"] = "local"
    wt = rig["wt"]
    (wt / ".gitignore").write_text("junk/\n")
    (wt / "junk").mkdir()
    (wt / "junk" / "big.bin").write_text("x" * 1000)
    assert worktree.remove(NAME, force=True) == 0
    (ref,) = _refs(rig["main"])
    names = _git(rig["main"], "ls-tree", "-r", "--name-only", ref).stdout.split()
    assert ".gitignore" in names and "junk/big.bin" not in names


@pytest.mark.spawns("sh")
def test_the_parked_work_outlives_the_worktree_and_a_gc(rig):
    # Linked worktrees share objects AND refs with main, so the ref keeps the commit reachable.
    rig["yard"]["mode"] = "local"
    main = rig["main"]
    (rig["wt"] / "new.txt").write_text("BRAND NEW\n")
    assert worktree.remove(NAME, force=True) == 0
    _git(main, "worktree", "prune")
    _git(main, "gc", "-q", "--prune=now")
    (ref,) = _refs(main)
    assert _git(main, "show", f"{ref}:new.txt").stdout == "BRAND NEW\n"


# ── what counts as uncommitted: "after unstaging everything, is anything different?" ─────────
#
# The worktree's index is deleted with it, so what it says is not the question; the files are.
# The check compares the working tree with HEAD through a throwaway index — the same tree the
# save would commit — so a stale index (the one dirty state seen on a real host, cleared by
# unstaging) reads clean, and a `--force` on anything it calls dirty always parks something.


def _commit_behind_the_index(wt: Path, name: str, text: str) -> None:
    """Move the worktree's branch to a commit of ``name`` = ``text`` (on disk too) WITHOUT the
    shared index learning of it — what a box's commit (its own index-box, ADR-0021) leaves behind
    on your computer's side."""
    (wt / name).write_text(text)
    env = {**os.environ, "GIT_INDEX_FILE": str(wt.parent / "other-index")}
    git = ["git", "-C", str(wt), "-c", "core.hooksPath=/dev/null", "-c", "filter.x.clean="]
    _real_run([*git, "read-tree", "HEAD"], env=env, check=True)
    _real_run([*git, "add", "-A"], env=env, check=True)
    tree = _real_run([*git, "write-tree"], env=env, capture_output=True, text=True).stdout.strip()
    oid = _real_run(
        [*git, "commit-tree", tree, "-p", "HEAD", "-m", "from the box"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    _real_run([*git, "update-ref", f"refs/heads/wt/{NAME}", oid], check=True)


@pytest.mark.spawns("sh")
def test_a_stale_index_is_not_uncommitted_work(rig):
    rig["yard"]["mode"] = "local"
    wt = rig["wt"]
    _commit_behind_the_index(wt, "y", "committed in the box\n")
    # The control: git's own definition calls it dirty (the index still holds the old `y`).
    assert _git(wt, "-c", "filter.x.clean=", "status", "--porcelain").stdout.strip()
    assert worktree.remove(NAME, assume_yes=True) == 0
    assert _gone(rig) and _refs(rig["main"]) == []


@pytest.mark.spawns("sh")
def test_a_real_edit_behind_a_stale_index_still_counts(rig):
    rig["yard"]["mode"] = "local"
    wt = rig["wt"]
    _commit_behind_the_index(wt, "y", "committed in the box\n")
    (wt / "x").write_text("EDITED\n")
    assert worktree.remove(NAME, assume_yes=True) == 1
    assert (wt / "x").read_text() == "EDITED\n"
    assert worktree.remove(NAME, force=True) == 0
    (ref,) = _refs(rig["main"])
    assert _git(rig["main"], "show", f"{ref}:x").stdout == "EDITED\n"
    assert _git(rig["main"], "show", f"{ref}:y").stdout == "committed in the box\n"


@pytest.mark.spawns("sh")
def test_an_ignored_file_is_not_uncommitted_work(rig):
    rig["yard"]["mode"] = "local"
    (rig["main"] / ".git" / "info" / "exclude").write_text("junk/\n")
    (rig["wt"] / "junk").mkdir()
    (rig["wt"] / "junk" / "big.bin").write_text("x" * 1000)
    assert worktree.remove(NAME, assume_yes=True) == 0
    assert _gone(rig) and _refs(rig["main"]) == []


@pytest.mark.spawns("sh")
def test_work_only_in_the_index_is_not_protected(rig):
    # Staged, then put back on disk: unstaging everything discards it, and the save never kept it
    # (it commits the files). Documented in docs/git.md — pinned so it stays a decision.
    rig["yard"]["mode"] = "local"
    wt = rig["wt"]
    (wt / "y").write_text("staged\n")
    _git(wt, "-c", "core.hooksPath=/dev/null", "-c", "filter.x.clean=", "add", "y")
    (wt / "y").write_text("plain\n")
    assert worktree.remove(NAME, assume_yes=True) == 0
    assert _gone(rig) and _refs(rig["main"]) == []


@pytest.mark.spawns("sh")
def test_the_check_leaves_the_worktrees_index_alone(rig):
    rig["yard"]["mode"] = "local"
    _dirty(rig)
    index = rig["main"] / ".git" / "worktrees" / NAME / "index"
    before = index.read_bytes()
    assert worktree._uncommitted_in_yard(rig["main"], rig["wt"], NAME, _YARD, save=False) == (
        "dirty",
        "",
    )
    assert index.read_bytes() == before
