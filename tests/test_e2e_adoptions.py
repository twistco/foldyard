"""The live modules forget what they adopted on your computer (tests/e2e_adoptions.py).

Every host-tier module adopts a throwaway copy of the example into the REAL adoption root, since
its CLI runs as the operator's would. Nothing forgot those, so `fy ls` listed 82 copies whose
folders pytest had long since deleted (2026-11-11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import e2e_adoptions


def _pin(root: Path, name: str, checkout: Path, worktree: str = "") -> Path:
    pin = root / name
    pin.mkdir(parents=True)
    doc = {"checkout": str(checkout), "worktree": worktree}
    (pin / "adopted.json").write_text(json.dumps(doc))
    (pin / "generations").mkdir()
    return pin


@pytest.fixture
def adoptions(tmp_path, monkeypatch):
    root = tmp_path / "adopted"
    root.mkdir()
    monkeypatch.setattr(e2e_adoptions, "_bases", set())
    return root


def test_forgets_the_copy_and_its_worktrees_and_nothing_else(adoptions, tmp_path):
    copy = tmp_path / "mod0" / "example"
    mine = _pin(adoptions, "example-1", copy)
    wt = _pin(adoptions, "feat-2", tmp_path / "mod0" / "example-worktrees" / "feat", "feat")
    yours = _pin(adoptions, "Tangible-3", tmp_path / "workspace" / "Tangible")
    beside = _pin(adoptions, "example-4", tmp_path / "mod00" / "example")  # a prefix, not a parent

    e2e_adoptions.record(copy)
    dropped = e2e_adoptions.forget_recorded(adoptions)

    assert sorted(dropped) == sorted([mine, wt])
    assert not mine.exists() and not wt.exists()
    assert yours.is_dir() and beside.is_dir()


def test_forgets_once_then_nothing_is_recorded(adoptions, tmp_path):
    copy = tmp_path / "mod0" / "example"
    e2e_adoptions.record(copy)
    _pin(adoptions, "example-1", copy)
    assert e2e_adoptions.forget_recorded(adoptions)
    again = _pin(adoptions, "example-2", copy)  # the next module's, at a path reused
    assert e2e_adoptions.forget_recorded(adoptions) == []
    assert again.is_dir()


def test_a_link_in_the_root_is_unlinked_never_followed(adoptions, tmp_path):
    copy = tmp_path / "mod0" / "example"
    elsewhere = _pin(tmp_path / "outside", "real", copy)
    link = adoptions / "example-1"
    link.symlink_to(elsewhere)

    e2e_adoptions.record(copy)
    assert e2e_adoptions.forget_recorded(adoptions) == [link]
    assert not link.is_symlink() and (elsewhere / "adopted.json").is_file()


def test_refuses_a_copy_straight_under_your_home(adoptions, tmp_path, monkeypatch):
    # Its parent would be your home, and every project you have lives under it.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(AssertionError):
        e2e_adoptions.record(tmp_path / "example")


def test_refuses_a_copy_straight_in_the_shared_temp_folder(adoptions, tmp_path, monkeypatch):
    # The temp folder is everyone's: clearing it would take other runs' adoptions with it.
    monkeypatch.setattr(e2e_adoptions.tempfile, "gettempdir", lambda: str(tmp_path))
    with pytest.raises(AssertionError):
        e2e_adoptions.record(tmp_path / "example")


# ── a copy reusing a path starts from that path's clean state ───────────────────────────────
# The host tier makes every module's copy at ONE path (tests/e2e_host.py), and what your computer
# keys by a checkout's path outlives the files: a later module's `fy worktree add feat` found an
# earlier module's `feat` "already registered".


def test_a_reused_paths_state_goes_and_no_other_projects(adoptions, tmp_path):
    from foldyard import worktree_registry

    copy = tmp_path / "host-tier" / "example"
    feat = tmp_path / "host-tier" / "example-worktrees" / "feat"
    yours = tmp_path / "workspace" / "Tangible"
    for repo in (copy, feat, yours, yours.parent / "Tangible-worktrees" / "feat"):
        (repo / ".git").mkdir(parents=True)
    worktree_registry.register(copy, "feat", feat)
    worktree_registry.register(yours, "feat", yours.parent / "Tangible-worktrees" / "feat")
    for main in (copy, yours):  # the offsets and the pooled ports beside each registry
        worktree_registry.offsets_file(main).write_text("{}")
        worktree_registry.ports_file(main).write_text("{}")
    mine = _pin(adoptions, "example-1", copy)
    mine_wt = _pin(adoptions, "feat-2", feat, "feat")
    theirs = _pin(adoptions, "Tangible-3", yours)
    beside = _pin(adoptions, "example2-4", tmp_path / "host-tier" / "example2")  # same parent

    e2e_adoptions.forget_checkout_state(copy, adoptions)

    assert worktree_registry.names(copy) == []
    assert not worktree_registry.offsets_file(copy).exists()
    assert not worktree_registry.ports_file(copy).exists()
    assert not mine.exists() and not mine_wt.exists()
    assert worktree_registry.names(yours) == ["feat"]
    assert worktree_registry.offsets_file(yours).exists()
    assert worktree_registry.ports_file(yours).exists()
    assert theirs.is_dir()
    assert beside.is_dir()


def test_each_fresh_host_tier_copy_starts_from_its_paths_clean_state(
    adoptions, tmp_path, monkeypatch
):
    import test_e2e
    from foldyard import worktree_registry

    monkeypatch.setattr(test_e2e, "_IN_BOX", False)
    monkeypatch.setattr(e2e_adoptions.configpin, "_pin_root", lambda: adoptions)
    copy = tmp_path / "host-tier" / "example"
    feat = tmp_path / "host-tier" / "example-worktrees" / "feat"
    (copy / ".git").mkdir(parents=True)
    (feat / ".git").mkdir(parents=True)
    worktree_registry.register(copy, "feat", feat)  # an earlier module's

    test_e2e.fresh_example(copy)

    assert worktree_registry.names(copy) == []
    assert (copy / "foldyard.toml").is_file()
