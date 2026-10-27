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


def _on_disk(tmp_path, *names):
    main = tmp_path / "repo"
    main.mkdir()
    wt_root = config.worktrees_root(main)
    for name in names:
        (wt_root / name).mkdir(parents=True)
        (wt_root / name / ".git").write_text("gitdir: x\n")
    (wt_root / "not-a-checkout").mkdir(parents=True)
    return main, wt_root


def test_registered_in_an_older_box_lists_the_worktrees_root_for_reporting(tmp_path, monkeypatch):
    # A box created before the host baked its registry in (no FY_WORKTREE_OFFSETS): the checkouts
    # on disk stand in, and none can be told apart as unregistered.
    main, _ = _on_disk(tmp_path, "a", "b")
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.delenv(crossmask.SNAPSHOT_ENV, raising=False)
    assert crossmask.baked_names() is None
    assert set(crossmask.registered(main)) == {"a", "b"}
    assert crossmask.unregistered(main) == {}


def test_registered_in_the_box_is_what_the_host_had_registered_at_create(tmp_path, monkeypatch):
    # The real report: two checkouts made with plain `git worktree add`, in a box created seconds
    # earlier. The host never masks those, so they aren't "added after the box was created".
    main, wt_root = _on_disk(tmp_path, "a", "refresh-curated-orgs", "sec-data-deps")
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setenv(crossmask.SNAPSHOT_ENV, '{"a": 7, "gone": 12}')
    assert crossmask.registered(main) == {"a": wt_root / "a"}  # never a name not on disk
    assert crossmask.unregistered(main) == {
        "refresh-curated-orgs": wt_root / "refresh-curated-orgs",
        "sec-data-deps": wt_root / "sec-data-deps",
    }


def test_an_empty_snapshot_registers_nothing_in_the_box(tmp_path, monkeypatch):
    # `{}` is the host saying "no worktrees registered", not an older box.
    main, _ = _on_disk(tmp_path, "a")
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setenv(crossmask.SNAPSHOT_ENV, "{}")
    assert crossmask.baked_names() == frozenset()
    assert crossmask.registered(main) == {}
    assert set(crossmask.unregistered(main)) == {"a"}


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", '"a"'])
def test_an_unreadable_snapshot_reads_as_an_older_box(tmp_path, monkeypatch, raw):
    main, _ = _on_disk(tmp_path, "a")
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setenv(crossmask.SNAPSHOT_ENV, raw)
    assert crossmask.baked_names() is None
    assert set(crossmask.registered(main)) == {"a"}
    assert crossmask.unregistered(main) == {}


@pytest.mark.parametrize("name", ["feat", "x$(touch pwned)", "a b", "it's", "v1.2_rc"])
def test_a_name_fy_worktree_add_takes_is_registrable(name):
    assert crossmask.registrable(name)


@pytest.mark.parametrize(
    "name", ["", "main", "worktrees", ".", "..", "a/b", "-rf", "--yes", "x\x1b[2J"]
)
def test_a_name_fy_worktree_add_refuses_or_reads_as_an_option_is_not(name):
    # The names come from the box-writable worktrees root, and the hint is a command to RUN on your
    # computer: never suggest one that can't succeed, or that isn't what it looks like.
    assert not crossmask.registrable(name)


def test_on_the_host_the_snapshot_is_never_read(tmp_path, monkeypatch, register_worktree):
    # Your computer has the registry itself: the env var (an operator's shell, say) changes
    # nothing, and its doctor names unregistered checkouts in its own `worktrees` row.
    main, wt_root = _on_disk(tmp_path, "planted")
    register_worktree(main, "real", wt_root / "real")
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setenv(crossmask.SNAPSHOT_ENV, '{"planted": 1}')
    assert set(crossmask.registered(main)) == {"real"}
    assert crossmask.unregistered(main) == {}


# ── a running box's live mounts, and the dirs the host makes for an added mask ─────────

_VOLROOT = "/home/u.guest/.local/share/containers/storage/volumes/acme-elsewhere/_data"


def _mi(mountpoint: str, root: str = _VOLROOT, opts: str = "ro,nosuid,nodev,relatime") -> str:
    return f"591 590 0:35 {root} {mountpoint} {opts} - btrfs /dev/vda2 rw,seclabel\n"


def test_mountinfo_masked_reads_the_note_volume_read_only_at_each_target():
    text = (
        "1 0 0:1 / / rw - overlay overlay rw\n"
        + _mi("/w/a/node_modules")
        + _mi("/w/b/node_modules", opts="rw,relatime")  # writable: no mask
        + _mi("/w/c/node_modules", root="/home/u.guest/other/_data")  # another volume
        # CoreOS shows the ostree deployment's path: still that volume's _data
        + _mi("/w/d/node_modules", root="/ostree/deploy/fcos/var/home/core" + _VOLROOT[13:])
        + _mi("/w/with\\040space/node_modules")
    )
    paths = [f"/w/{n}/node_modules" for n in ("a", "b", "c", "d", "e", "with space")]
    assert crossmask.mountinfo_masked(text, paths, "acme-elsewhere") == {
        "/w/a/node_modules",
        "/w/d/node_modules",
        "/w/with space/node_modules",
    }


def test_mountinfo_masked_takes_the_mount_on_top():
    # A later mount at the same point covers the earlier one; what the box sees is the top one.
    covered = _mi("/w/a/node_modules") + _mi("/w/a/node_modules", root="/x", opts="rw")
    assert crossmask.mountinfo_masked(covered, ["/w/a/node_modules"], "acme-elsewhere") == set()
    uncovered = _mi("/w/a/node_modules", root="/x", opts="rw") + _mi("/w/a/node_modules")
    assert crossmask.mountinfo_masked(uncovered, ["/w/a/node_modules"], "acme-elsewhere") == {
        "/w/a/node_modules"
    }


def test_mountinfo_masked_ignores_lines_it_cannot_read():
    assert crossmask.mountinfo_masked("garbage\n\n1 2\n", ["/w/a"], "acme-elsewhere") == set()


def test_host_dir_walks_from_the_bind_root_that_holds_the_mask(tmp_path):
    main = tmp_path / "repo"
    wt_root = tmp_path / "repo-worktrees"
    assert crossmask.host_dir(f"{wt_root}/a/platform/node_modules", main, wt_root) == (
        wt_root,
        "a/platform/node_modules",
    )
    assert crossmask.host_dir(f"{main}/data/.venv", main, wt_root) == (main, "data/.venv")
    assert crossmask.host_dir("/elsewhere/x", main, wt_root) is None


def test_host_dir_prefers_the_deeper_root_when_the_worktrees_live_in_main(tmp_path):
    # From the worktrees root, never from main: the walk then checks the worktree's own dir too.
    main = tmp_path / "repo"
    wt_root = main / ".worktrees"
    assert crossmask.host_dir(f"{wt_root}/a/node_modules", main, wt_root) == (
        wt_root,
        "a/node_modules",
    )
