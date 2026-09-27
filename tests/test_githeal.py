"""githeal — the box proposes a healed shared index, the host only renames it into place.

Box-side commands run through the ACTUAL shim (test_git_shim.Rig), so the proposals the host
consumes are produced by the real mechanism. Every host-side heal here runs with process creation
FORBIDDEN (`heal`): the host runs no git — nor anything else — in the box-writable checkout.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from foldyard import githeal
from test_git_shim import Rig


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.host_commit("init", fileA="a1\n", fileB="b1\n", fileD="d1\n")
    return r


def heal(repo, main=None):
    """One host-side heal pass with every way to start a process refused — a spawn fails the
    test by name, whatever the heal does with the exception."""

    def refuse(*args, **_kw):
        raise AssertionError(f"the host-side heal started a process: {args[1:2] or args[:1]}")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(subprocess.Popen, "__init__", refuse)
        for name in ("system", "posix_spawn", "posix_spawnp", "fork", "execv", "execve"):
            if hasattr(os, name):
                mp.setattr(os, name, refuse)
        return githeal.heal_checkout(repo, main)


def _box_commit(rig, msg, **files):
    for name, content in files.items():
        rig.write(name, content)
    assert rig.box("add", "--", *files).returncode == 0
    r = rig.box("commit", "-qm", msg)
    assert r.returncode == 0, r.stderr
    return rig.head()


def _host_status(rig):
    return rig.host("status", "--porcelain").stdout


def _pending(rig) -> list[str]:
    return sorted(p.name for p in rig.gitdir.glob("index.fy-*") if p.name != githeal.SYNC_FILE)


def _synced_at(rig, head: str) -> None:
    """Bring the shared index's sync marker to ``head`` the way it happens for real: a box git
    call notices the host's own HEAD move and says so, and the host records it."""
    rig.box("status", "--porcelain")
    heal(rig.repo)
    assert (rig.gitdir / githeal.SYNC_FILE).read_text().strip() == head


def test_heals_shared_index_after_box_commit(rig):
    # A box commit moves shared HEAD, the host's index still describes the old one → phantom
    # staged-D/?? and MM pairs in every host-side git GUI. The box proposes; one pass installs.
    _box_commit(rig, "box", fileA="a2\n", fileC="c1\n")
    assert "fileA" in _host_status(rig)  # the illusion is really there before the heal
    assert [p for p in _pending(rig) if p.startswith("index.fy-proposed.")]
    msg = heal(rig.repo)
    assert msg and "fast-forwarded" in msg
    assert _host_status(rig) == ""
    assert (rig.gitdir / githeal.SYNC_FILE).read_text().strip() == rig.head()
    assert _pending(rig) == []  # consumed
    assert heal(rig.repo) is None  # steady state is a quiet no-op


def test_host_moved_head_only_resyncs_the_marker(rig):
    # The host's own git already manages its index — an unattributed HEAD move must never touch
    # it. In particular `git reset --soft` (index ≠ HEAD BY DESIGN) survives untouched.
    _synced_at(rig, rig.head())
    rig.host_commit("host C2", fileA="a2\n")
    _synced_at(rig, rig.head())
    rig.host("reset", "-q", "--soft", "HEAD~")
    _synced_at(rig, rig.head())
    assert "M  fileA" in _host_status(rig)  # the soft reset's staged state is intact


def test_staged_host_work_is_carried_forward(rig):
    # Host has a staged modification, a staged deletion, and a staged add; the box commits a
    # DISJOINT change. The proposal replays the host's staged entries on the new HEAD's tree.
    _synced_at(rig, rig.head())  # also seeds index-box at C1 BEFORE the host stages anything
    rig.write("fileB", "b-host\n")
    rig.write("fileE", "e1\n")
    rig.host("add", "--", "fileB", "fileE")
    rig.host("rm", "-q", "--cached", "fileD")
    _box_commit(rig, "box", fileA="a2\n")
    msg = heal(rig.repo)
    assert msg and "carried forward" in msg
    status = _host_status(rig)
    assert "M  fileB" in status and "A  fileE" in status and "D  fileD" in status
    assert "fileA" not in status  # the box's commit is absorbed, not phantom-staged


@pytest.mark.parametrize("var", [None, "GIT_GLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"])
def test_a_carried_path_is_matched_literally_never_as_a_glob(rig, var):
    # The carry re-stages the host's staged paths by name. Read as a pathspec, `file*` also
    # matches `fileA` — whose old entry would then land on the new HEAD's tree: a proposal that
    # silently stages a revert of the box's commit. The shim inherits the box user's environment,
    # so a global pathspec setting there must neither re-widen the match nor clash with the
    # literal matching (git refuses `literal` beside any other global setting: a false refusal).
    _synced_at(rig, rig.head())
    rig.write("file*", "star\n")
    rig.host("add", "--", ":(literal)file*")
    rig.write("fileA", "a2\n")
    assert rig.box("add", "--", "fileA").returncode == 0
    r = rig.box("commit", "-qm", "box", env={var: "1"} if var else None)
    assert r.returncode == 0, r.stderr
    msg = heal(rig.repo)
    assert msg and "carried forward" in msg
    assert _host_status(rig) == "A  file*\n"


def test_overlapping_staged_work_refuses_once(rig):
    # Both sides touched fileA — never merge silently: say so once, leave the index alone, and
    # stay quiet until the state changes.
    _synced_at(rig, rig.head())
    rig.write("fileA", "a-host\n")
    rig.host("add", "--", "fileA")
    _box_commit(rig, "box", fileA="a-box\n")
    msg = heal(rig.repo)
    assert msg and "NOT healing" in msg
    assert heal(rig.repo) is None  # throttled repeat
    assert "fileA" in _host_status(rig)  # untouched, as promised
    # The documented manual fix clears it: the refusal goes stale with the index it judged.
    rig.host("reset", "-q")
    assert heal(rig.repo) is None
    assert _pending(rig) == []
    assert _host_status(rig) == ""


def test_a_proposal_built_on_an_index_the_host_since_changed_is_dropped(rig):
    # The host stages between the box's proposal and the tick: installing it would lose that
    # staging. The proposal names the index it was built from; a mismatch drops it, and the box
    # proposes again (from the index as it now is) on its next git call.
    _synced_at(rig, rig.head())
    _box_commit(rig, "box", fileA="a2\n")
    rig.write("fileB", "b-host\n")
    rig.host("add", "--", "fileB")
    assert heal(rig.repo) is None
    assert _pending(rig) == []  # dropped, not installed
    assert "M  fileB" in _host_status(rig)
    rig.box("status", "--porcelain")  # the box's next git call re-proposes
    msg = heal(rig.repo)
    assert msg and "carried forward" in msg
    assert _host_status(rig) == "M  fileB\n"


def test_a_proposal_for_a_head_the_host_moved_away_from_is_dropped(rig):
    # A soft reset moves HEAD without changing the index — the base still matches, but installing
    # the box's tree now would stage the undone commit. HEAD must still be the proposal's.
    _box_commit(rig, "box", fileA="a2\n")
    rig.host("reset", "-q", "--soft", "HEAD~")
    before = (rig.gitdir / "index").read_bytes()
    assert heal(rig.repo) is None
    assert (rig.gitdir / "index").read_bytes() == before
    assert not [p for p in _pending(rig) if p.startswith("index.fy-proposed.")]


@pytest.mark.parametrize("when", ["before the lock", "during the write"])
def test_a_head_moved_while_installing_is_never_left_installed(rig, monkeypatch, when):
    # index.lock guards the index, not refs: a soft reset can land between the pass reading HEAD
    # and the proposal being written. Checked again under the lock, and once more after the write
    # (then the index the host had is put back) — never a recorded heal to a HEAD that has gone.
    before = rig.head()
    ref = rig.repo / ".git" / rig.host("symbolic-ref", "HEAD").stdout.strip()
    new = _box_commit(rig, "box", fileA="a2\n")
    shared = (rig.gitdir / "index").read_bytes()
    moved, written = [], []

    def soft_reset():  # as `git reset --soft HEAD~`: the ref moves, the index is untouched
        if not moved:
            moved.append(True)
            ref.write_text(before + "\n")

    def write(m, rel, data, _real=githeal.mountwrite.write):
        _real(m, rel, data)
        if rel.endswith("/index"):
            written.append(data)
            if when == "during the write":
                soft_reset()

    monkeypatch.setattr(githeal.mountwrite, "write", write)
    if when == "before the lock":
        resolve = githeal._resolve_head
        monkeypatch.setattr(githeal, "_resolve_head", lambda *a: (resolve(*a), soft_reset())[0])
    assert heal(rig.repo) is None
    assert moved and rig.head() == before != new
    assert (rig.gitdir / "index").read_bytes() == shared
    # Caught under the lock → nothing written at all; caught after → the proposal, then put back.
    assert len(written) == (0 if when == "before the lock" else 2)
    sync = rig.gitdir / githeal.SYNC_FILE
    assert not sync.exists() or sync.read_text().strip() != new


def test_skips_in_progress_operations_and_lock(rig):
    _box_commit(rig, "box", fileA="a2\n")
    (rig.gitdir / "MERGE_HEAD").write_text(rig.head() + "\n")
    assert heal(rig.repo) is None
    (rig.gitdir / "MERGE_HEAD").unlink()
    (rig.gitdir / "index.lock").write_text("")
    assert heal(rig.repo) is None
    (rig.gitdir / "index.lock").unlink()
    assert heal(rig.repo) is not None  # heals once the coast is clear


def test_non_repo_and_unborn_are_quiet(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert heal(empty) is None
    subprocess.run(["git", "init", "-q", str(tmp_path / "unborn")], check=True)
    assert heal(tmp_path / "unborn") is None


def test_the_shim_proposes_nothing_with_healing_off(rig):
    rig.write("fileA", "a2\n")
    rig.box("add", "--", "fileA", env={"FY_GIT_SHIM_NO_HEAL": "1"})
    assert rig.box("commit", "-qm", "box", env={"FY_GIT_SHIM_NO_HEAL": "1"}).returncode == 0
    assert _pending(rig) == []


def test_sweep_gates_on_index_split_and_never_raises(monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr(githeal.config, "box_git_index_split", lambda: False)
    monkeypatch.setattr(
        githeal, "_checkouts", lambda: pytest.fail("split off → must not even resolve checkouts")
    )
    githeal.sweep(logged.append)
    assert logged == []

    monkeypatch.setattr(githeal.config, "box_git_index_split", lambda: True)
    monkeypatch.setattr(githeal, "_checkouts", lambda: (githeal.Path("/m"), [githeal.Path("/m")]))
    monkeypatch.setattr(
        githeal, "heal_checkout", lambda co, main: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    githeal.sweep(logged.append)  # must not raise — the reconcile loop depends on it
    assert logged and "sweep failed" in logged[0]


# ── the host runs nothing, and writes only where it means to ─────────────────────────────


def test_a_heal_runs_nothing_the_mount_plants(rig, tmp_path):
    # `.git/config` and the attributes are box-writable: git would run `core.fsmonitor` or a
    # clean filter as a command. The heal must not merely neutralise them — it runs no git at all
    # (`heal` refuses any process), and the planted canaries stay unwritten.
    _box_commit(rig, "box", fileA="a2\n", fileC="c1\n")
    canary = tmp_path / "PWNED"
    (rig.repo / ".gitattributes").write_text("* filter=evil\n")
    subprocess.run(
        ["git", "-C", str(rig.repo), "config", "filter.evil.clean", f"sh -c ': > {canary}; cat'"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(rig.repo), "config", "core.fsmonitor", f"sh -c ': > {canary}'"],
        check=True,
    )
    msg = heal(rig.repo)
    assert msg and "fast-forwarded" in msg
    assert not canary.exists()


def test_a_symlinked_proposal_is_never_installed(rig, tmp_path):
    # A proposal is a box-written file in the git dir; one that is a symlink would have the host
    # install (and later rewrite) a file of the box's choosing outside it.
    _synced_at(rig, rig.head())
    _box_commit(rig, "box", fileA="a2\n")
    prop = next(p for p in rig.gitdir.glob("index.fy-proposed.*"))
    outside = tmp_path / "outside-index"
    outside.write_bytes(prop.read_bytes())
    prop.unlink()
    prop.symlink_to(outside)
    before = (rig.gitdir / "index").read_bytes()
    assert heal(rig.repo) is None
    assert (rig.gitdir / "index").read_bytes() == before
    assert not (rig.gitdir / "index").is_symlink()


def test_a_symlinked_git_dir_is_not_followed(rig, tmp_path):
    # `.git` itself is box-writable: pointed at a directory of the operator's, a heal would read
    # and write there.
    elsewhere = tmp_path / "elsewhere"
    head = rig.head()
    (rig.repo / ".git").rename(elsewhere)
    (rig.repo / ".git").symlink_to(elsewhere)
    # A record for the REAL HEAD there — one a followed symlink would act on.
    (elsewhere / f"index.fy-record.{head}").write_text("")
    assert heal(rig.repo) is None
    assert not (elsewhere / githeal.SYNC_FILE).exists()  # (mountwrite would refuse this anyway)
    assert (elsewhere / f"index.fy-record.{head}").exists()  # …but nothing there was consumed


def test_a_worktree_git_file_pointing_outside_mains_worktrees_is_refused(rig, tmp_path):
    # A worktree's `.git` is a box-writable FILE naming its git dir. Only `<main>/.git/worktrees/*`
    # is ever this project's — anything else (another checkout's git dir) is not healed.
    wt = tmp_path / "wt"
    rig.host("worktree", "add", "-q", str(wt), "-b", "side")
    assert heal(wt, rig.repo) is None  # a real, confined worktree: quiet, no error
    (tmp_path / "o").mkdir()
    other = Rig(tmp_path / "o")  # a real repo with a HEAD, and a record the heal would act on
    other_head = other.host_commit("other", x="x\n")
    (other.gitdir / f"index.fy-record.{other_head}").write_text("")
    (wt / ".git").write_text(f"gitdir: {other.gitdir}\n")
    assert heal(wt, rig.repo) is None
    assert not (other.gitdir / githeal.SYNC_FILE).exists()


def test_a_worktree_heals_in_its_own_git_dir(rig, tmp_path):
    # The confined path works: a box commit in a worktree is healed in `<main>/.git/worktrees/<n>`.
    wt = tmp_path / "wt"
    rig.host("worktree", "add", "-q", str(wt), "-b", "side")
    wt_rig = Rig.__new__(Rig)
    wt_rig.__dict__.update(rig.__dict__, repo=wt)
    _box_commit(wt_rig, "box in wt", fileA="a-wt\n")
    msg = heal(wt, rig.repo)
    assert msg and "fast-forwarded" in msg
    assert wt_rig.host("status", "--porcelain").stdout == ""


def test_malformed_proposal_names_are_ignored(rig):
    (rig.gitdir / "index.fy-proposed.not-hex.x.ff").write_bytes(b"DIRC")
    assert heal(rig.repo) is None
    assert (rig.gitdir / "index.fy-proposed.not-hex.x.ff").exists()  # not ours — left alone


def test_the_sweep_visits_only_registered_worktrees(monkeypatch, tmp_path, register_worktree):
    # The worktrees root is box-writable: a dir (or a symlink to another checkout) planted there
    # must not become somewhere the supervisor heals.
    from foldyard import config, devmode

    main = tmp_path / "repo"
    (main / ".git").mkdir(parents=True)
    root = tmp_path / "repo-worktrees"
    register_worktree(main, "known", root / "known")
    (root / "planted" / ".git").mkdir(parents=True)
    monkeypatch.setattr(devmode, "main_repo", lambda: main)
    monkeypatch.setattr(config, "worktrees_root", lambda _base: root)
    assert githeal._checkouts() == (main, [main, (root / "known").resolve()])
