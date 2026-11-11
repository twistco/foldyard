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
