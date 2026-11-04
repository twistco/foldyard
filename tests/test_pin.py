"""``just pin <commit>`` (tests/tools/pin.py): ``fy`` frozen at a commit, whatever the checkout does.

Real git against a scratch repo, which is the point: what it must survive is a repo a box can write.
Only ``uv tool install`` is stubbed (no uv in the hermetic suite); its argv is pinned on its own."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools import pin

Install = list[Path]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, content: str, subject: str = "a commit") -> str:
    (repo / "code.py").write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", subject)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """A repo with no remote: everything in it is unpushed by definition."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _commit(r, "good\n", "good")
    return r


@pytest.fixture
def pin_dir(tmp_path):
    return tmp_path / "fy-pin"


@pytest.fixture
def installed() -> Install:
    return []


@pytest.fixture
def run(repo, pin_dir, installed):
    def _run(commit: str, source: Path | None = None) -> pin.Pinned:
        return pin.pin(commit, source or repo, pin_dir, install=installed.append)

    return _run


def _pinned(pin_dir: Path) -> str:
    return (pin_dir / "code.py").read_text()


# ── what it pins ──────────────────────────────────────────────────────────────────────────────


def test_a_full_hash_is_checked_out_in_the_clone_and_installed_from_there(
    repo, pin_dir, installed, run
):
    sha = _git(repo, "rev-parse", "HEAD")
    assert run(sha).commit == sha
    assert _git(pin_dir, "rev-parse", "HEAD") == sha
    assert _pinned(pin_dir) == "good\n"
    assert installed == [pin_dir]


def test_a_short_hash_resolves_to_the_commit(repo, pin_dir, run):
    sha = _git(repo, "rev-parse", "HEAD")
    assert run(sha[:8]).commit == sha


def test_an_uppercase_hash_is_the_same_commit(repo, run):
    sha = _git(repo, "rev-parse", "HEAD")
    assert run(sha.upper()).commit == sha


def test_a_commit_on_another_branch(repo, pin_dir, run):
    _git(repo, "checkout", "-qb", "feature")
    sha = _commit(repo, "feature\n")
    _git(repo, "checkout", "-q", "main")
    run(sha)
    assert _pinned(pin_dir) == "feature\n"


def test_a_commit_on_no_branch_at_all(repo, pin_dir, run):
    parent = _git(repo, "rev-parse", "HEAD")
    sha = _commit(repo, "dangling\n")
    _git(repo, "reset", "-q", "--hard", parent)
    assert _git(repo, "branch", "--contains", sha) == ""
    run(sha)
    assert _pinned(pin_dir) == "dangling\n"


def test_a_commit_made_in_a_worktree(repo, tmp_path, pin_dir, run):
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "wt", str(wt))
    sha = _commit(wt, "from a worktree\n")
    run(sha)
    assert _pinned(pin_dir) == "from a worktree\n"


def test_uncommitted_work_is_not_in_the_pin(repo, pin_dir, run):
    sha = _git(repo, "rev-parse", "HEAD")
    (repo / "code.py").write_text("uncommitted\n")
    run(sha)
    assert _pinned(pin_dir) == "good\n"


def test_the_checkout_moving_on_leaves_the_pin_alone(repo, pin_dir, run):
    sha = _git(repo, "rev-parse", "HEAD")
    run(sha)
    _commit(repo, "later\n")
    _git(repo, "checkout", "-qb", "elsewhere")
    assert _pinned(pin_dir) == "good\n"
    assert _git(pin_dir, "rev-parse", "HEAD") == sha


def test_repinning_says_what_it_replaced(repo, pin_dir, run):
    first = _git(repo, "rev-parse", "HEAD")
    assert run(first).previous is None
    second = _commit(repo, "second\n", "the second one")
    pinned = run(second)
    assert (pinned.commit, pinned.previous, pinned.subject) == (second, first, "the second one")
    assert _pinned(pin_dir) == "second\n"


def test_an_existing_clones_origin_is_kept(repo, tmp_path, pin_dir, run):
    """Pointing the clone at GitHub is how you say only pushed commits count."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _git(elsewhere, "init", "-q", "-b", "main")
    sha = _commit(elsewhere, "from elsewhere\n")
    pin_dir.mkdir()
    _git(pin_dir, "init", "-q")
    _git(pin_dir, "remote", "add", "origin", str(elsewhere))
    run(sha)
    assert _pinned(pin_dir) == "from elsewhere\n"
    assert _git(pin_dir, "remote", "get-url", "origin") == str(elsewhere)


# ── refusals ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name", ["main", "HEAD", "origin/main", "abc123", "", "--upload-pack=x", "g" * 40, "a" * 41]
)
def test_only_a_hash_is_accepted(name, run, pin_dir, installed):
    """A name is something a box can move, so it resolves to a commit you never looked at."""
    with pytest.raises(pin.Refused, match="commit hash"):
        run(name)
    assert not pin_dir.exists()
    assert installed == []


def test_a_short_hash_nothing_has_is_refused(run, pin_dir, installed):
    with pytest.raises(pin.Refused, match="no commit"):
        run("0000000")
    assert not pin_dir.exists()
    assert installed == []


def test_a_full_hash_nothing_has_fails_at_the_fetch_and_installs_nothing(run, installed):
    with pytest.raises(pin.Refused, match="fetch"):
        run("0" * 40)
    assert installed == []


def test_a_directory_that_isnt_a_clone_is_left_alone(repo, pin_dir, run, installed):
    pin_dir.mkdir()
    (pin_dir / "precious").write_text("mine\n")
    with pytest.raises(pin.Refused, match="isn't a clone"):
        run(_git(repo, "rev-parse", "HEAD"))
    assert sorted(p.name for p in pin_dir.iterdir()) == ["precious"]
    assert installed == []


def test_a_pin_inside_the_repo_is_refused(repo, installed):
    """The clone exists to be out of the box's reach."""
    with pytest.raises(pin.Refused, match="outside"):
        pin.pin(
            _git(repo, "rev-parse", "HEAD"), repo, repo / "sub" / "pin", install=installed.append
        )
    assert not (repo / "sub").exists()
    assert installed == []


# ── a failed install ──────────────────────────────────────────────────────────────────────────


def _failing_install(_pin_dir: Path) -> None:
    raise pin.Refused("the install failed")


def test_a_failed_install_puts_the_previous_pin_back(repo, pin_dir, run):
    first = _git(repo, "rev-parse", "HEAD")
    run(first)
    second = _commit(repo, "second\n")
    with pytest.raises(pin.Refused, match="still pinned to " + first):
        pin.pin(second, repo, pin_dir, install=_failing_install)
    assert _git(pin_dir, "rev-parse", "HEAD") == first
    assert _pinned(pin_dir) == "good\n"


def test_a_failed_first_install_says_nothing_is_pinned(repo, pin_dir):
    with pytest.raises(pin.Refused, match="not pinned"):
        pin.pin(_git(repo, "rev-parse", "HEAD"), repo, pin_dir, install=_failing_install)


# ── a repo the box has been writing ───────────────────────────────────────────────────────────


def test_a_branch_named_after_the_hash_isnt_followed(repo, pin_dir, run):
    """Git tries a ref before a short hash, so `rev-parse <short>` would take this branch."""
    good = _git(repo, "rev-parse", "HEAD")
    evil = _commit(repo, "evil\n", "evil")
    _git(repo, "reset", "-q", "--hard", good)
    short = good[:8]
    _git(repo, "branch", short, evil)
    assert _git(repo, "rev-parse", short) == evil  # the control: git itself would follow it
    assert run(short).commit == good
    assert _pinned(pin_dir) == "good\n"


def test_a_replace_ref_cant_change_what_a_hash_means(repo, pin_dir, run):
    good = _git(repo, "rev-parse", "HEAD")
    evil = _commit(repo, "evil\n", "evil")
    _git(repo, "reset", "-q", "--hard", good)
    _git(repo, "replace", good, evil)
    assert _git(repo, "show", f"{good}:code.py") == "evil"  # the control
    pinned = run(good)
    assert _pinned(pin_dir) == "good\n"
    assert pinned.subject == "good"


def test_the_hash_is_a_commit_not_any_object(repo, run):
    blob = _git(repo, "rev-parse", "HEAD:code.py")
    with pytest.raises(pin.Refused, match="no commit"):
        run(blob[:10])


def test_a_short_hash_that_matches_two_commits_is_refused(repo, run):
    """Nothing can look it up unambiguously, and picking one is the box's choice."""
    prefix = _git(repo, "rev-parse", "HEAD")[:7]
    with pytest.raises(pin.Refused, match="more than one"):
        pin.resolve(repo, prefix, candidates=lambda _r, _p: ["a" * 40, "b" * 40])


def _trap(path: Path, mark: Path) -> Path:
    path.write_text(f'#!/bin/sh\n: > "{mark}"\ncat >/dev/null\nexit 1\n')
    path.chmod(0o755)
    return path


def _arm(repo: Path, tmp_path: Path) -> Path:
    """Everything in the repo's own config that could run code, each leaving a mark."""
    marks = tmp_path / "marks"
    marks.mkdir()
    bin_dir = tmp_path / "traps"
    bin_dir.mkdir()
    hooks = bin_dir / "hooks"
    hooks.mkdir()
    for hook in ("post-checkout", "reference-transaction", "post-index-change"):
        _trap(hooks / hook, marks / f"hook-{hook}")
    _git(repo, "config", "core.hooksPath", str(hooks))
    _git(repo, "config", "core.fsmonitor", str(_trap(bin_dir / "fsmonitor", marks / "fsmonitor")))
    _git(repo, "config", "core.sshCommand", str(_trap(bin_dir / "ssh", marks / "ssh")))
    _git(
        repo,
        "config",
        "uploadpack.packObjectsHook",
        str(_trap(bin_dir / "packhook", marks / "packhook")),
    )
    lazy = _trap(bin_dir / "lazy", marks / "lazy")
    _git(repo, "config", "protocol.ext.allow", "always")
    _git(repo, "config", "extensions.partialClone", "origin")
    _git(repo, "remote", "add", "origin", f"ext::{lazy}")
    _git(repo, "config", "remote.origin.promisor", "true")
    _git(repo, "config", "remote.origin.partialclonefilter", "blob:none")
    return marks


def _clear(marks: Path) -> None:
    for m in marks.iterdir():
        m.unlink()


def _with_a_missing_blob(repo: Path) -> tuple[str, str]:
    """A commit on its own branch whose blob is gone: what a lazy fetch would go after."""
    _git(repo, "checkout", "-qb", "bait")
    (repo / "lost.txt").write_text("lost\n")
    _git(repo, "add", "lost.txt")
    _git(repo, "commit", "-qm", "bait")
    bait, blob = _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD:lost.txt")
    _git(repo, "checkout", "-q", "main")
    (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    return bait, blob


def test_nothing_in_the_repos_config_runs(repo, tmp_path, pin_dir, run):
    good = _git(repo, "rev-parse", "HEAD")
    bait, blob = _with_a_missing_blob(repo)
    marks = _arm(repo, tmp_path)

    # the controls: these traps are live for an ordinary git command in that repo
    subprocess.run(["git", "-C", str(repo), "status"], capture_output=True)
    assert (marks / "fsmonitor").exists()
    subprocess.run(["git", "-C", str(repo), "cat-file", "-p", blob], capture_output=True)
    assert (marks / "lazy").exists()
    _clear(marks)

    # other branches' damage doesn't reach a pin that doesn't need them
    run(good[:10])
    assert _pinned(pin_dir) == "good\n"
    assert sorted(m.name for m in marks.iterdir()) == []

    # a commit that does need the missing blob: refused, and no lazy fetch tried for it
    with pytest.raises(pin.Refused, match="fetch"):
        run(bait)
    assert sorted(m.name for m in marks.iterdir()) == []
    assert _git(pin_dir, "rev-parse", "HEAD") == good


def test_the_clone_takes_no_config_from_the_repo(repo, tmp_path, pin_dir, run):
    _arm(repo, tmp_path)
    run(_git(repo, "rev-parse", "HEAD"))
    config = (pin_dir / ".git" / "config").read_text()
    for needle in ("hooksPath", "fsmonitor", "sshCommand", "promisor", "ext::"):
        assert needle not in config


def test_control_characters_in_a_subject_dont_reach_the_terminal(repo, pin_dir, run):
    """A commit message is box-written, and this prints it."""
    sha = _commit(repo, "x\n", "fine\x1b]52;c;bad\x07\x1b[2Jstill fine")
    report = pin.report(run(sha))
    assert "\x1b" not in report and "\x07" not in report
    assert "fine" in report and "still fine" in report


# ── the rest of the shape ─────────────────────────────────────────────────────────────────────


def test_the_install_is_editable_from_the_clone_and_run_from_outside_the_checkout(
    pin_dir, monkeypatch
):
    """Editable, or the box's `fy` comes from PyPI (box._foldyard_install_subst). Run from `/`, or
    uv reads the checkout's `uv.toml`, which can name another package index."""
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw.get("cwd")))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pin.subprocess, "run", fake_run)
    pin.install(pin_dir)
    assert calls == [(["uv", "tool", "install", "--force", "--editable", str(pin_dir)], "/")]


def test_a_failing_install_is_a_refusal_with_uvs_reason(pin_dir, monkeypatch):
    monkeypatch.setattr(
        pin.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "the real reason\x1b[2J\n"),
    )
    with pytest.raises(pin.Refused, match="the real reason") as e:
        pin.install(pin_dir)
    assert "\x1b" not in str(e.value)


def test_the_directory_defaults_under_your_home_and_the_env_moves_it(monkeypatch, tmp_path):
    monkeypatch.delenv("FY_PIN_DIR", raising=False)
    assert pin.pin_dir() == Path.home() / ".local" / "share" / "fy-pin"
    monkeypatch.setenv("FY_PIN_DIR", str(tmp_path / "x"))
    assert pin.pin_dir() == tmp_path / "x"


def test_the_box_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("IN_DEVBOX", "1")
    assert pin.main(["0" * 40]) == 1
    assert "box" in capsys.readouterr().err


def test_main_finds_the_repo_from_a_worktree_and_reports(
    repo, tmp_path, pin_dir, monkeypatch, capsys
):
    monkeypatch.delenv("IN_DEVBOX", raising=False)
    monkeypatch.setenv("FY_PIN_DIR", str(pin_dir))
    monkeypatch.setattr(pin, "install", lambda _d: None)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "wt", str(wt))
    sha = _commit(wt, "from a worktree\n", "worktree commit")
    monkeypatch.chdir(wt)
    assert pin.main([sha]) == 0
    out = capsys.readouterr().out
    assert sha in out and "worktree commit" in out and "fy host restart" in out
    # origin is the main repo, not a worktree that may be removed
    assert os.path.realpath(_git(pin_dir, "remote", "get-url", "origin")) == os.path.realpath(repo)


def test_main_reports_a_refusal_and_fails(repo, pin_dir, monkeypatch, capsys):
    monkeypatch.delenv("IN_DEVBOX", raising=False)
    monkeypatch.setenv("FY_PIN_DIR", str(pin_dir))
    monkeypatch.chdir(repo)
    assert pin.main(["main"]) == 1
    assert "commit hash" in capsys.readouterr().err


def test_the_recipe_runs_this_tool():
    justfile = (Path(__file__).parent.parent / "justfile").read_text()
    assert "\npin commit:\n" in justfile
    assert "tests/tools/pin.py {{quote(commit)}}" in justfile
