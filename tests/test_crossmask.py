"""crossmask — the read-only masks a dev box mounts over OTHER checkouts' dependency dirs (#91)."""

from __future__ import annotations

from pathlib import Path

import pytest

from foldyard import config, crossmask
from foldyard.crossmask import Mask

DIRS = ["data/.venv", "platform/node_modules"]


@pytest.fixture
def layout(tmp_path):
    main = tmp_path / "repo"
    wt_root = tmp_path / "repo-worktrees"
    for p in (main, wt_root / "a", wt_root / "b"):
        p.mkdir(parents=True)
    return main, wt_root, {"a": wt_root / "a", "b": wt_root / "b"}


def test_main_box_masks_every_registered_worktrees_dirs_and_not_its_own(layout):
    main, wt_root, wts = layout
    masks, refused = crossmask.targets(main, main, wt_root, wts, DIRS)
    assert refused == []
    assert masks == [
        Mask("a", f"{wt_root}/a/data/.venv"),
        Mask("a", f"{wt_root}/a/platform/node_modules"),
        Mask("b", f"{wt_root}/b/data/.venv"),
        Mask("b", f"{wt_root}/b/platform/node_modules"),
    ]


def test_worktree_box_masks_main_and_its_siblings_but_not_itself(layout):
    main, wt_root, wts = layout
    masks, _ = crossmask.targets(wt_root / "a", main, wt_root, wts, DIRS)
    assert [m.checkout for m in masks] == ["", "", "b", "b"]
    assert Mask("", f"{main}/data/.venv") in masks
    assert not [m for m in masks if m.path.startswith(f"{wt_root}/a/")]


def test_nothing_to_mask_without_declared_dirs_or_other_checkouts(layout):
    main, wt_root, wts = layout
    assert crossmask.targets(main, main, wt_root, wts, []) == ([], [])
    assert crossmask.targets(main, main, wt_root, {}, DIRS) == ([], [])


def test_registry_paths_are_real_paths_and_map_back_onto_the_mounted_root(tmp_path):
    # The registry records REAL paths; the box sees the worktrees root at the path it was given.
    real_root = tmp_path / "real-worktrees"
    (real_root / "a").mkdir(parents=True)
    linked = tmp_path / "linked-worktrees"
    linked.symlink_to(real_root)
    main = tmp_path / "repo"
    main.mkdir()
    masks, _ = crossmask.targets(main, main, linked, {"a": real_root / "a"}, ["node_modules"])
    assert masks == [Mask("a", f"{linked}/a/node_modules")]


def test_a_worktree_the_box_does_not_bind_is_not_masked(layout, tmp_path):
    # Registered somewhere outside both the main checkout and the worktrees root: the box can't
    # reach it, so there's nothing to mask (and a mount target there would CREATE the path).
    main, wt_root, _ = layout
    elsewhere = tmp_path / "elsewhere" / "c"
    elsewhere.mkdir(parents=True)
    masks, _ = crossmask.targets(main, main, wt_root, {"c": elsewhere}, DIRS)
    assert masks == []


def test_a_target_an_engine_cannot_parse_is_refused_not_mounted(tmp_path):
    main = tmp_path / "repo"
    wt_root = tmp_path / "wts"
    for p in (main, wt_root / "x:y", wt_root / "p,q"):
        p.mkdir(parents=True)
    wts = {"x:y": wt_root / "x:y", "p,q": wt_root / "p,q"}
    masks, refused = crossmask.targets(main, main, wt_root, wts, ["node_modules"])
    assert masks == []
    assert refused == [f"{wt_root}/p,q/node_modules", f"{wt_root}/x:y/node_modules"]


def test_a_dir_holding_the_boxs_own_checkout_is_never_masked(tmp_path):
    # The worktrees root inside the main checkout, and a "shadow dir" that holds it: covering it
    # would hide the box's own files.
    main = tmp_path / "repo"
    wt_root = main / ".worktrees"
    (wt_root / "a").mkdir(parents=True)
    masks, _ = crossmask.targets(wt_root / "a", main, wt_root, {"a": wt_root / "a"}, [".worktrees"])
    assert masks == []


def test_entries_that_cannot_name_a_dir_in_the_checkout_are_dropped(layout):
    main, wt_root, wts = layout
    masks, _ = crossmask.targets(main, main, wt_root, wts, ["", "/", "../out", "/web/"])
    assert [m.path for m in masks] == [f"{wt_root}/a/web", f"{wt_root}/b/web"]


@pytest.mark.parametrize("entry", [".", "./.", "./", "a/..", "./a/../."])
def test_an_entry_naming_the_checkout_root_never_masks_a_whole_checkout(layout, entry):
    # `<other>/.` is the other checkout itself: a mask there would make all of it read-only.
    main, wt_root, wts = layout
    assert crossmask.targets(main, main, wt_root, wts, [entry]) == ([], [])


def test_entries_are_normalised_before_they_become_mount_targets(layout):
    main, wt_root, wts = layout
    masks, _ = crossmask.targets(main, main, wt_root, {"a": wts["a"]}, ["./web/./node_modules"])
    assert masks == [Mask("a", f"{wt_root}/a/web/node_modules")]


def test_run_args_mount_the_note_volume_read_only():
    masks = [Mask("a", "/w/a/node_modules"), Mask("", "/repo/node_modules")]
    assert crossmask.run_args("acme-elsewhere", masks) == [
        "-v",
        "acme-elsewhere:/w/a/node_modules:ro",
        "-v",
        "acme-elsewhere:/repo/node_modules:ro",
    ]


def test_note_volume_is_per_project_and_outside_the_worktree_volume_sweep(monkeypatch):
    monkeypatch.setenv("FOLDYARD_PROJECT_PREFIX", "acme")
    assert crossmask.note_volume() == "acme-elsewhere"
    # `fy worktree remove` sweeps `<prefix>-<name>-devbox-*`; no worktree name reaches this one.
    assert not crossmask.note_volume().startswith("acme-elsewhere-devbox-")


def _vol(dest: str, name: str = "acme-elsewhere", rw: bool = False) -> dict:
    return {"Type": "volume", "Name": name, "Destination": dest, "RW": rw}


def test_unmasked_names_the_masks_a_running_box_lacks():
    masks = [Mask("a", "/w/a/node_modules"), Mask("b", "/w/b/node_modules")]
    mounts = [
        _vol("/w/a/node_modules"),
        {"Type": "bind", "Source": "/w/b", "Destination": "/w/b/node_modules", "RW": True},
        _vol("/w/b/node_modules", name="something-else"),
    ]
    assert crossmask.unmasked(mounts, masks, "acme-elsewhere") == [masks[1]]
    assert crossmask.unmasked(None, masks, "acme-elsewhere") == masks


@pytest.mark.parametrize("rw", [True, None])
def test_a_note_volume_mounted_writable_is_not_a_mask(rw):
    # Only a read-only mount stops an install; a writable one (or one the engine doesn't
    # describe) lets it land in the shared note volume instead.
    mask = Mask("a", "/w/a/node_modules")
    mount = {"Type": "volume", "Name": "acme-elsewhere", "Destination": mask.path}
    if rw is not None:
        mount["RW"] = rw
    assert crossmask.unmasked([mount], [mask], "acme-elsewhere") == [mask]


def test_unshadowed_names_own_dirs_without_a_volume():
    mounts = [_vol("/repo/data/.venv", name="acme-devbox-shadow-data--venv", rw=True)]
    assert crossmask.unshadowed(mounts, "/repo", DIRS) == ["platform/node_modules"]
    assert crossmask.unshadowed(mounts, "/repo/", ["data/.venv"]) == []


def test_the_note_names_both_command_forms_and_where_each_checkout_lives(tmp_path):
    text = crossmask.note_text(Path("/home/me/acme"), Path("/home/me/acme-worktrees"))
    assert "WORKTREE=<name> fy box exec '<command>'" in text
    assert "WORKTREE= fy box exec '<command>'" in text
    assert "WORKTREE=<name> fy box up" in text and "on your computer" in text
    assert "/home/me/acme-worktrees/<name>/" in text and "/home/me/acme/" in text
    assert "Mac" not in text


def test_the_fill_script_writes_the_note_atomically_and_world_readable():
    script = crossmask.fill_script()
    assert f"{crossmask.FILL_AT}/{crossmask.NOTE_FILE}" in script
    assert "chmod 0444" in script and "mv -f" in script
    assert script.startswith(f"rm -f {crossmask.FILL_AT}/{crossmask.NOTE_FILE}.tmp && ")


def test_registered_on_the_host_is_the_registry_never_a_listing(
    tmp_path, monkeypatch, register_worktree
):
    main = tmp_path / "repo"
    main.mkdir()
    wt_root = config.worktrees_root(main)
    (wt_root / "planted").mkdir(parents=True)
    (wt_root / "planted" / ".git").write_text("gitdir: x\n")
    register_worktree(main, "real", wt_root / "real")
    monkeypatch.setattr(config, "in_box", lambda: False)
    assert set(crossmask.registered(main)) == {"real"}


def test_registered_in_the_box_lists_the_worktrees_root_for_reporting(tmp_path, monkeypatch):
    main = tmp_path / "repo"
    main.mkdir()
    wt_root = config.worktrees_root(main)
    for name in ("a", "b"):
        (wt_root / name).mkdir(parents=True)
        (wt_root / name / ".git").write_text("gitdir: x\n")
    (wt_root / "not-a-checkout").mkdir()
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert set(crossmask.registered(main)) == {"a", "b"}
