"""githeal — the supervisor's host-side twin of the shim's self-heal, against REAL git.

Box-side commands run through the ACTUAL shim (test_git_shim.Rig), so the attribution stamp
these heals key on is produced by the real mechanism, not a hand-rolled simulation.
"""

from __future__ import annotations

import subprocess

import pytest

from foldyard import githeal
from test_git_shim import Rig


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.host_commit("init", fileA="a1\n", fileB="b1\n", fileD="d1\n")
    return r


def _box_commit(rig, msg, **files):
    for name, content in files.items():
        rig.write(name, content)
    assert rig.box("add", "--", *files).returncode == 0
    r = rig.box("commit", "-qm", msg)
    assert r.returncode == 0, r.stderr
    return rig.head()


def _host_status(rig):
    return rig.host("status", "--porcelain").stdout


def test_heals_shared_index_after_box_commit(rig):
    # THE Mac-side fix: a box commit moves shared HEAD, the host's index still describes the
    # old one → phantom staged-D/?? and MM pairs in every GUI. One heal pass → clean.
    _box_commit(rig, "box", fileA="a2\n", fileC="c1\n")
    assert "fileA" in _host_status(rig)  # the illusion is really there before the heal
    msg = githeal.heal_checkout(rig.repo)
    assert msg and "fast-forwarded" in msg
    assert _host_status(rig) == ""
    assert (rig.gitdir / githeal.SYNC_FILE).read_text().strip() == rig.head()
    assert githeal.heal_checkout(rig.repo) is None  # steady state is a quiet no-op


def test_host_moved_head_only_resyncs_the_marker(rig):
    # The host's own git already manages its index — an unattributed HEAD move must never be
    # touched. In particular `git reset --soft` (index ≠ HEAD BY DESIGN) survives untouched.
    githeal.heal_checkout(rig.repo)  # marker at C1
    rig.host_commit("host C2", fileA="a2\n")
    assert githeal.heal_checkout(rig.repo) is None
    rig.host("reset", "-q", "--soft", "HEAD~")
    assert githeal.heal_checkout(rig.repo) is None
    assert "M  fileA" in _host_status(rig)  # the soft reset's staged state is intact
    assert (rig.gitdir / githeal.SYNC_FILE).read_text().strip() == rig.head()


def test_staged_host_work_is_carried_forward(rig):
    # Host has a staged modification, a staged deletion, and a staged add; the box commits a
    # DISJOINT change. The heal replays the host's staged entries on the new HEAD's tree.
    githeal.heal_checkout(rig.repo)  # marker at C1
    rig.box("status", "--porcelain")  # seed index-box at C1 — BEFORE the host stages anything
    # (a first box touch seeds from the shared index, staged state included, by design —
    # without this the box commit would legitimately absorb the host's staged work)
    rig.write("fileB", "b-host\n")
    rig.write("fileE", "e1\n")
    rig.host("add", "--", "fileB", "fileE")
    rig.host("rm", "-q", "--cached", "fileD")
    _box_commit(rig, "box", fileA="a2\n")
    msg = githeal.heal_checkout(rig.repo)
    assert msg and "carried forward" in msg
    status = _host_status(rig)
    assert "M  fileB" in status and "A  fileE" in status and "D  fileD" in status
    assert "fileA" not in status  # the box's commit is absorbed, not phantom-staged


def test_overlapping_staged_work_refuses_once(rig):
    # Both sides touched fileA — never merge silently: warn once (throttled), leave the index
    # alone, and stay quiet until the state changes.
    githeal.heal_checkout(rig.repo)
    rig.write("fileA", "a-host\n")
    rig.host("add", "--", "fileA")
    _box_commit(rig, "box", fileA="a-box\n")
    msg = githeal.heal_checkout(rig.repo)
    assert msg and "NOT healing" in msg
    assert githeal.heal_checkout(rig.repo) is None  # throttled repeat
    assert "fileA" in _host_status(rig)  # untouched, as promised
    # The documented manual fix clears it and the marker resyncs on the next pass.
    rig.host("reset", "-q")
    assert githeal.heal_checkout(rig.repo) is None
    assert _host_status(rig) == ""


def test_skips_in_progress_operations_and_lock(rig):
    _box_commit(rig, "box", fileA="a2\n")
    (rig.gitdir / "MERGE_HEAD").write_text(rig.head() + "\n")
    assert githeal.heal_checkout(rig.repo) is None
    (rig.gitdir / "MERGE_HEAD").unlink()
    (rig.gitdir / "index.lock").write_text("")
    assert githeal.heal_checkout(rig.repo) is None
    (rig.gitdir / "index.lock").unlink()
    assert githeal.heal_checkout(rig.repo) is not None  # heals once the coast is clear


def test_non_repo_and_unborn_are_quiet(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert githeal.heal_checkout(empty) is None
    subprocess.run(["git", "init", "-q", str(tmp_path / "unborn")], check=True)
    assert githeal.heal_checkout(tmp_path / "unborn") is None


def test_sweep_gates_on_index_split_and_never_raises(monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr(githeal.config, "box_git_index_split", lambda: False)
    monkeypatch.setattr(
        githeal, "_checkouts", lambda: pytest.fail("split off → must not even resolve checkouts")
    )
    githeal.sweep(logged.append)
    assert logged == []

    monkeypatch.setattr(githeal.config, "box_git_index_split", lambda: True)
    monkeypatch.setattr(githeal, "_checkouts", lambda: [1])
    monkeypatch.setattr(
        githeal, "heal_checkout", lambda co: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    githeal.sweep(logged.append)  # must not raise — the reconcile loop depends on it
    assert logged and "sweep failed" in logged[0]
