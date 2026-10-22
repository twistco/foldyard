"""boxmounts — ADR-0033's two foldyard-owned host directories: where they are, which VM pairs get a
to-box, and the host-side writes into the box-writable from-box that must never follow a link."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from foldyard import boxmounts, config


@pytest.fixture
def state(tmp_path, monkeypatch):
    s = tmp_path / "state"
    monkeypatch.setattr(config, "state_dir", lambda: s)
    monkeypatch.setattr(config, "in_box", lambda: False)
    return s


@pytest.fixture
def victim(tmp_path):
    v = tmp_path / "outside" / "id_rsa"
    v.parent.mkdir()
    v.write_text("TOPSECRET")
    return v


def test_the_two_roots_sit_in_the_state_dir(state):
    assert boxmounts.to_box_dir() == state / "to-box"
    assert boxmounts.from_box_dir() == state / "from-box"


def test_slots_mirror_the_posture_dirs_so_a_worktree_named_main_cant_collide():
    assert boxmounts.slot_rel("") == Path("main")
    assert boxmounts.slot_rel("main") == Path("worktrees/main")
    assert boxmounts.claude_rel("feat") == Path("worktrees/feat/claude/projects")
    assert boxmounts.codex_rel("") == Path("main/codex/sessions")


@pytest.mark.parametrize("name", ["..", ".", "a/b", "/abs"])
def test_a_worktree_name_that_isnt_one_path_component_names_no_slot(name):
    with pytest.raises(ValueError):
        boxmounts.slot_rel(name)


@pytest.mark.parametrize("name", [".hidden", "fix+x", "x y", "feat@2"])
def test_any_name_fy_worktree_add_allows_gets_its_slot(name):
    assert boxmounts.slot_rel(name) == Path("worktrees") / name


@pytest.mark.parametrize(
    "vmtype, mount_type, enforced",
    [
        ("vz", "virtiofs", True),
        ("qemu", "9p", True),
        ("qemu", "virtiofs", False),  # virtiofsd runs without --readonly
        ("vz", "reverse-sshfs", False),
        ("krunkit", "virtiofs", False),
        ("", "", False),
    ],
)
def test_only_the_pairs_that_enforce_read_only_outside_the_guest_count(
    vmtype, mount_type, enforced
):
    assert boxmounts.to_box_enforced(vmtype, mount_type) is enforced


def test_the_pinned_mount_type_per_vmtype():
    assert boxmounts.mount_type_for("vz") == "virtiofs"
    assert boxmounts.mount_type_for("qemu") == "9p"
    assert boxmounts.mount_type_for("krunkit") == ""


def test_enabled_on_lima_only(monkeypatch):
    monkeypatch.setattr(config, "machine_backend", lambda: "lima")
    assert boxmounts.enabled()
    monkeypatch.setattr(config, "machine_backend", lambda: "podman")
    assert not boxmounts.enabled()


def test_nested_sees_through_a_symlinked_worktrees_root(tmp_path):
    # Review on #115: FOLDYARD_WORKTREES_ROOT isn't resolved; a link to a directory holding the
    # state dir overlaps it all the same.
    real = tmp_path / "real"
    (real / "state" / "to-box").mkdir(parents=True)
    link = tmp_path / "wt-link"
    link.symlink_to(real)
    assert boxmounts.nested([real / "state" / "to-box"], [link]) == [
        (real / "state" / "to-box", link)
    ]


def test_nested_finds_a_state_dir_inside_a_writable_mount(tmp_path):
    repo = tmp_path / "repo"
    inside = repo / ".fy" / "to-box"
    beside = tmp_path / "state" / "to-box"
    assert boxmounts.nested([inside, beside], [repo, tmp_path / "repo-worktrees"]) == [
        (inside, repo)
    ]
    # …and the other way round: a mount inside one of the directories
    assert boxmounts.nested([tmp_path], [repo]) == [(tmp_path, repo)]


def test_vm_paths_on_your_computer(state, monkeypatch):
    monkeypatch.setattr(config, "machine_backend", lambda: "lima")
    assert boxmounts.vm_paths() == (str(state / "from-box"), str(state / "to-box"))
    monkeypatch.setattr(config, "machine_backend", lambda: "podman")
    assert boxmounts.vm_paths() == ("", "")


def test_vm_paths_in_the_box_are_what_box_up_baked(monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setenv(boxmounts.ENV_FROM_BOX, "/s/from-box")
    monkeypatch.delenv(boxmounts.ENV_TO_BOX, raising=False)
    assert boxmounts.vm_paths() == ("/s/from-box", "")


def test_ensure_bind_source_makes_a_slot_without_following_a_planted_link(state, victim):
    boxmounts.prepare()
    root = boxmounts.from_box_dir()
    (root / "worktrees").mkdir()
    (root / "worktrees" / "feat").symlink_to(victim.parent)
    with pytest.raises(OSError):
        boxmounts.ensure_bind_source(root / boxmounts.claude_rel("feat"))
    assert sorted(p.name for p in victim.parent.iterdir()) == ["id_rsa"]


def test_ensure_bind_source_makes_a_fresh_slot(state):
    boxmounts.prepare()
    target = boxmounts.from_box_dir() / boxmounts.claude_rel("")
    boxmounts.ensure_bind_source(target)
    assert target.is_dir()


def test_ensure_bind_source_keeps_the_legacy_path_working(tmp_path, state):
    legacy = tmp_path / "repo" / "dev-stack" / ".devbox-claude" / "projects"
    boxmounts.ensure_bind_source(legacy)
    assert legacy.is_dir()


def test_prepare_makes_both_roots_private(state):
    boxmounts.prepare()
    for d in (boxmounts.to_box_dir(), boxmounts.from_box_dir()):
        assert d.is_dir() and d.stat().st_mode & 0o077 == 0


def test_remove_slot_deletes_the_worktrees_slot(state):
    boxmounts.prepare()
    slot = boxmounts.from_box_dir() / boxmounts.claude_rel("feat")
    boxmounts.ensure_bind_source(slot)
    (slot / "s.jsonl").write_text("{}")
    assert boxmounts.remove_slot("feat") is None
    assert not (boxmounts.from_box_dir() / "worktrees" / "feat").exists()
    assert (boxmounts.from_box_dir() / "worktrees").is_dir()  # siblings' parent kept


def test_remove_slot_unlinks_a_planted_link_and_leaves_its_target(state, victim):
    boxmounts.prepare()
    (boxmounts.from_box_dir() / "worktrees").mkdir()
    (boxmounts.from_box_dir() / "worktrees" / "feat").symlink_to(victim.parent)
    assert boxmounts.remove_slot("feat") is None
    assert victim.read_text() == "TOPSECRET"
    assert not (boxmounts.from_box_dir() / "worktrees" / "feat").is_symlink()


def test_remove_slot_never_follows_a_linked_worktrees_dir(state, victim):
    boxmounts.prepare()
    (victim.parent / "feat").mkdir()
    (boxmounts.from_box_dir() / "worktrees").symlink_to(victim.parent)
    why = boxmounts.remove_slot("feat")
    assert why and "not followed" in why
    assert (victim.parent / "feat").is_dir() and victim.read_text() == "TOPSECRET"


def test_remove_slot_refuses_where_rmtree_could_follow_a_link(state, monkeypatch):
    boxmounts.prepare()
    boxmounts.ensure_bind_source(boxmounts.from_box_dir() / boxmounts.claude_rel("feat"))
    monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", False)
    why = boxmounts.remove_slot("feat")
    assert why and "by hand" in why
    assert (boxmounts.from_box_dir() / "worktrees" / "feat").is_dir()


def test_remove_slot_with_nothing_there(state):
    assert boxmounts.remove_slot("feat") is None
    boxmounts.prepare()
    assert boxmounts.remove_slot("feat") is None


def test_remove_slot_never_takes_the_main_checkouts_slot(state):
    with pytest.raises(ValueError):
        boxmounts.remove_slot("")
