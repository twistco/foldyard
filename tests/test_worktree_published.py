"""worktree_published.py — the to-box list of the worktrees your computer registered, for a box."""

from __future__ import annotations

import json
import os

import pytest

from foldyard import (
    boxmounts,
    config,
    devmode,
    stack,
    supervisor,
    worktree,
    worktree_published,
)
from foldyard import worktree_registry as registry


@pytest.fixture
def main(tmp_path, monkeypatch):
    main = tmp_path / "repo"
    (main / ".git").mkdir(parents=True)
    to_box = tmp_path / "state" / "to-box"
    to_box.mkdir(parents=True)
    monkeypatch.setattr(boxmounts, "to_box_dir", lambda: to_box)
    monkeypatch.setattr(boxmounts, "TO_BOX_IN_BOX", str(to_box))  # the box's bind of it
    monkeypatch.setattr(registry, "store_file", lambda m: tmp_path / "reg" / "reg.json")
    monkeypatch.setattr(worktree_published.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "in_box", lambda: False)
    return main


def _register(main, name):
    path = main.parent / "wts" / name
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: x\n")
    registry.register(main, name, path)


def test_what_your_computer_publishes_is_what_the_box_reads(main):
    assert worktree_published.read(main) is None  # nothing published yet: unknown, never "none"
    worktree_published.publish(main)
    assert worktree_published.read(main) == frozenset()
    _register(main, "feat")
    worktree_published.publish(main)
    assert worktree_published.read(main) == {"feat"}


def test_one_whose_checkout_is_gone_is_still_registered(main):
    _register(main, "feat")
    (main.parent / "wts" / "feat" / ".git").unlink()
    worktree_published.publish(main)
    assert worktree_published.read(main) == {"feat"}


def test_an_unchanged_list_is_not_written_again(main):
    # A replaced file reads as missing in the guest for about a second, on every supervisor tick.
    worktree_published.publish(main)
    before = os.stat(worktree_published.path(main, boxmounts.to_box_dir())).st_ino
    worktree_published.publish(main)
    assert os.stat(worktree_published.path(main, boxmounts.to_box_dir())).st_ino == before


def test_another_clone_of_the_project_has_its_own_list(main, tmp_path):
    # Two clones of one project share its state dir.
    other = tmp_path / "other"
    _register(main, "feat")
    worktree_published.publish(main)
    assert worktree_published.read(other) is None


@pytest.mark.parametrize(
    "text", ["", "[]", "{}", '{"main": "/x", "registered": []}', '{"registered": ["a", 1]}']
)
def test_anything_else_in_the_file_is_unknown(main, text):
    worktree_published.publish(main)
    worktree_published.path(main, boxmounts.to_box_dir()).write_text(text)
    assert worktree_published.read(main) is None


def test_a_list_that_cannot_be_written_is_taken_away(main, monkeypatch):
    # A stale one would call a worktree just registered unregistered.
    worktree_published.publish(main)
    _register(main, "feat")

    def full(*a):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(worktree_published.os, "replace", full)
    worktree_published.publish(main)
    assert worktree_published.read(main) is None


@pytest.mark.parametrize("text", ["", "{", "[]", '{"feat": 3}'])
def test_a_registry_that_cannot_be_read_is_unknown_not_empty(main, text):
    worktree_published.publish(main)
    registry.store_file(main).parent.mkdir(exist_ok=True)
    registry.store_file(main).write_text(text)
    worktree_published.publish(main)
    assert worktree_published.read(main) is None


def test_two_publishers_cannot_leave_the_older_list(main, monkeypatch):
    # The supervisor reads the registry, `fy worktree add` registers and publishes, and then the
    # supervisor's older list would land on top.
    import threading

    real, first = registry.names, threading.Event()
    other: list[threading.Thread] = []

    def names(m):
        got = real(m)
        if not first.is_set():
            first.set()
            _register(main, "feat")
            other.append(threading.Thread(target=worktree_published.publish, args=(main,)))
            other[0].start()
            other[0].join(0.5)  # it waits for this one
        return got

    monkeypatch.setattr(registry, "names", names)
    worktree_published.publish(main)
    other[0].join(5)
    assert worktree_published.read(main) == {"feat"}


def test_no_to_box_is_not_made(main):
    boxmounts.to_box_dir().rmdir()
    worktree_published.publish(main)
    assert not boxmounts.to_box_dir().exists()


def test_a_box_publishes_nothing(main, monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    worktree_published.publish(main)
    assert worktree_published.read(main) is None


def test_a_replaced_file_reads_as_missing_for_a_moment(main, monkeypatch):
    # vz: about a second. The box looks again before it says unknown.
    worktree_published.publish(main)
    file = worktree_published.path(main, boxmounts.to_box_dir())
    text = file.read_text()
    file.unlink()
    monkeypatch.setattr(worktree_published.time, "sleep", lambda s: file.write_text(text))
    assert worktree_published.read(main) == frozenset()


def test_the_supervisor_keeps_it_current(main, monkeypatch):
    monkeypatch.setattr(devmode, "main_repo", lambda: main)
    _register(main, "feat")
    supervisor._publish_registered()
    assert worktree_published.read(main) == {"feat"}


def test_unregistering_publishes(main, monkeypatch):
    monkeypatch.setattr(worktree.stack, "main_repo", lambda: main)
    monkeypatch.setattr(config, "worktrees_root", lambda m: main.parent / "wts")
    monkeypatch.setattr(worktree, "_local_state_roots", lambda: [])
    _register(main, "feat")
    worktree_published.publish(main)
    worktree._remove_local_state("feat")
    assert worktree_published.read(main) == frozenset()


@pytest.fixture
def pooled(main, monkeypatch):
    monkeypatch.setattr(stack, "main_repo", lambda: main)
    monkeypatch.setattr(config, "port_bases", lambda: {"APP_PORT": 3000, "PG_PORT": 5533})
    monkeypatch.setattr(config, "port_pools", lambda: {"APP_PORT": range(3001, 3006)})
    monkeypatch.setattr(stack, "_pins", lambda main: {})
    monkeypatch.setattr(stack, "_hashed_offset", lambda name: 40)
    for var in ("WT_OFFSET", "FY_WORKTREE_OFFSETS", "FY_WORKTREE_PORTS"):
        monkeypatch.delenv(var, raising=False)
    return main


def test_the_file_holds_names_ports_and_offsets_only(pooled):
    _register(pooled, "feat")
    stack.claim_ports("feat")
    worktree_published.publish(pooled)
    data = json.loads(worktree_published.path(pooled, boxmounts.to_box_dir()).read_text())
    assert data == {
        "main": str(pooled),
        "registered": ["feat"],
        "ports": {"feat": {"APP_PORT": 3001}},
        "offsets": {"feat": 40},
    }


def _in_a_box(monkeypatch, ports="{}", offsets="{}"):
    """A box made before any worktree, which is what it was given at create."""
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setenv("FY_WORKTREE_PORTS", ports)
    monkeypatch.setenv("FY_WORKTREE_OFFSETS", offsets)


def test_a_running_box_knows_the_ports_of_a_worktree_registered_since(pooled, monkeypatch):
    # `WORKTREE=feat fy box exec …` and `fy ps` were refused, with `fy box recreate` as the advice,
    # which ends every session in the box.
    _register(pooled, "feat")
    stack.claim_ports("feat")
    monkeypatch.setattr(stack, "_recorded_offsets", lambda main, pins: {"feat": 17})
    worktree_published.publish(pooled)
    _in_a_box(monkeypatch)
    assert stack._published_ports("feat") == {"APP_PORT": "3001", "PG_PORT": str(5533 + 17)}


def test_what_the_box_was_given_at_create_still_comes_first(pooled, monkeypatch):
    # A port that moved since needs the box recreated (`fy box up` says so): it doesn't half follow.
    _register(pooled, "feat")
    stack.claim_ports("feat")
    worktree_published.publish(pooled)
    _in_a_box(monkeypatch, ports='{"feat": {"APP_PORT": 3004}}', offsets='{"feat": 9}')
    assert stack._published_ports("feat") == {"APP_PORT": "3004", "PG_PORT": str(5533 + 9)}


def test_one_the_list_says_is_not_registered_is_told_so(pooled, monkeypatch, capsys):
    worktree_published.publish(pooled)
    _in_a_box(monkeypatch)
    monkeypatch.setattr(config, "worktrees_root", lambda m: pooled.parent / "wts")
    with pytest.raises(SystemExit):
        stack._published_ports("plain")
    err = capsys.readouterr().err
    assert "isn't registered on your computer" in err and "fy worktree add plain" in err
    assert "fy box recreate" not in err  # it can't help


def test_with_no_list_both_ways_out_are_named(pooled, monkeypatch, capsys):
    _in_a_box(monkeypatch)
    monkeypatch.setattr(config, "worktrees_root", lambda m: pooled.parent / "wts")
    with pytest.raises(SystemExit):
        stack._published_ports("later")
    err = capsys.readouterr().err
    assert "fy box recreate" in err and "fy worktree add later" in err


def test_ports_that_cannot_be_worked_out_leave_the_names(pooled, monkeypatch):
    _register(pooled, "feat")
    monkeypatch.setattr(stack, "worktree_ports", lambda: 1 / 0)
    worktree_published.publish(pooled)
    assert worktree_published.read(pooled) == {"feat"}
    _in_a_box(monkeypatch)
    with pytest.raises(stack.PortsUnknown):
        stack.worktree_port("APP_PORT", "feat")


@pytest.mark.parametrize("bad", ['"x"', "[1]", '{"feat": {"APP_PORT": "3001"}}', '{"feat": 3}'])
def test_ports_that_are_not_ports_are_unknown(pooled, monkeypatch, bad):
    _register(pooled, "feat")
    worktree_published.publish(pooled)
    file = worktree_published.path(pooled, boxmounts.to_box_dir())
    file.write_text(json.dumps({**json.loads(file.read_text()), "ports": json.loads(bad)}))
    _in_a_box(monkeypatch)
    with pytest.raises(stack.PortsUnknown):
        stack.worktree_port("APP_PORT", "feat")
