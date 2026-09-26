"""worktree_registry — the host's own record of which worktrees are this project's.

The worktrees root is on the box-writable mount and the box holds the engine socket, so neither a
directory nor a container is evidence the host may act on: a checkout counts only when the host
recorded it (``fy worktree add``) and it is still the real directory that was recorded."""

from __future__ import annotations

import pytest

from foldyard import worktree_registry as reg


@pytest.fixture
def main(tmp_path):
    m = tmp_path / "repo"
    (m / ".git").mkdir(parents=True)
    return m


def _checkout(path):
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: elsewhere\n")
    return path


def test_nothing_is_registered_until_the_host_records_it(main, tmp_path):
    _checkout(tmp_path / "repo-worktrees" / "feat")  # a dir with .git is NOT a registration
    assert reg.checkouts(main) == {}


def test_register_roundtrip_records_the_real_path(main, tmp_path):
    wt = _checkout(tmp_path / "repo-worktrees" / "feat")
    reg.register(main, "feat", wt)
    assert reg.checkouts(main) == {"feat": wt.resolve()}
    reg.unregister(main, "feat")
    assert reg.checkouts(main) == {}


def test_the_store_is_keyed_by_the_main_checkout_not_by_config(main, tmp_path):
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    reg.register(main, "feat", _checkout(tmp_path / "repo-worktrees" / "feat"))
    assert reg.checkouts(other) == {}


def test_a_registered_dir_swapped_for_a_symlink_no_longer_counts(main, tmp_path):
    # The box can replace a registered checkout with a symlink to another checkout on the host
    # (whose ADOPTED config and state dir would then be reconciled for a box-controlled worktree).
    wt = _checkout(tmp_path / "repo-worktrees" / "feat")
    reg.register(main, "feat", wt)
    elsewhere = _checkout(tmp_path / "someone-elses-checkout")
    (wt / ".git").unlink()
    wt.rmdir()
    wt.symlink_to(elsewhere)
    assert reg.checkouts(main) == {}


def test_a_removed_checkout_no_longer_counts(main, tmp_path):
    wt = _checkout(tmp_path / "repo-worktrees" / "feat")
    reg.register(main, "feat", wt)
    (wt / ".git").unlink()
    assert reg.checkouts(main) == {}


def test_a_symlinked_dir_cannot_be_registered(main, tmp_path):
    target = _checkout(tmp_path / "someone-elses-checkout")
    link = tmp_path / "repo-worktrees" / "feat"
    link.parent.mkdir()
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        reg.register(main, "feat", link)


def test_a_damaged_store_registers_nothing(main, tmp_path):
    reg.register(main, "feat", _checkout(tmp_path / "repo-worktrees" / "feat"))
    reg.store_file(main).write_text("{not json")
    assert reg.checkouts(main) == {}


def test_unregistered_lists_checkout_dirs_the_host_does_not_know(main, tmp_path):
    root = tmp_path / "repo-worktrees"
    reg.register(main, "known", _checkout(root / "known"))
    _checkout(root / "stray")
    (root / "not-a-checkout").mkdir()
    assert reg.unregistered(main, root) == ["stray"]


def test_a_symlinked_ancestor_is_the_operators_layout_and_is_fine(main, tmp_path):
    real_ws = tmp_path / "volume" / "ws"
    wt = _checkout(real_ws / "feat")
    (tmp_path / "ws-link").symlink_to(real_ws)
    reg.register(main, "feat", tmp_path / "ws-link" / "feat")
    assert reg.checkouts(main) == {"feat": wt.resolve()}
