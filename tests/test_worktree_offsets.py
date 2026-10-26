"""A worktree's port offset: allocated by the host, never shared.

The offset was a hash of the worktree's name into 89 slots, and nothing noticed two names landing on
one: with 8 worktrees that's a 28% chance. Tangible's ``sec-platform-fixes`` and ``sec-supabase-url``
both hash to 48 (2026-10-26), so their proxies wanted one port and the supervisor's reaper killed
each in turn. The host now records an offset per registered worktree (``worktree_registry``): the
hashed slot while it's free, so a worktree that never collided never moves, else a free one."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from foldyard import config, stack
from foldyard import worktree_registry as reg

# ── the allocation itself (pure) ──────────────────────────────────────────────────────────────


def _hash(table: dict[str, int]):
    return lambda name: table[name]


def test_two_names_on_one_hashed_slot_get_two_offsets():
    got = reg.allocate(["fixes", "supabase"], {}, {}, _hash({"fixes": 48, "supabase": 48}))
    assert got["fixes"] == 48  # the first by name keeps its hashed slot
    assert got["supabase"] != 48 and 1 <= got["supabase"] <= 89


def test_a_worktree_that_never_collided_never_moves():
    # The one that moves takes a slot nobody hashes to — never the next worktree's own slot, which
    # would move a worktree that never collided (and strand its box on its old port).
    hashes = {"a": 48, "b": 48, "c": 49, "d": 50}
    got = reg.allocate(sorted(hashes), {}, {}, _hash(hashes))
    assert (got["a"], got["c"], got["d"]) == (48, 49, 50)
    assert got["b"] not in (48, 49, 50)


def test_a_recorded_offset_is_kept_and_a_newcomer_goes_around_it():
    got = reg.allocate(["new", "old"], {"old": 48}, {}, _hash({"new": 48, "old": 48}))
    assert got["old"] == 48 and got["new"] != 48


def test_pinned_offsets_are_never_handed_out_and_a_pin_moves_a_recorded_one():
    got = reg.allocate(["a", "b"], {}, {"pinned": 5}, _hash({"a": 5, "b": 7}))
    assert got["a"] != 5 and got["b"] == 7
    # A pin taken AFTER a worktree was given that offset: the pin wins, the other one moves.
    got = reg.allocate(["a", "pinned"], {"a": 5}, {"pinned": 5}, _hash({"a": 5}))
    assert got == {"a": got["a"]} and got["a"] != 5


def test_a_full_band_leaves_the_rest_unallocated():
    names = [f"w{i}" for i in range(95)]
    got = reg.allocate(names, {}, {}, lambda name: 1)
    assert sorted(got.values()) == list(range(1, 90))  # the caller falls back to the hash for 6


@st.composite
def _worlds(draw):
    """Registered names with their hashes (dense, so collisions are common), some already
    recorded, and some pins — plain data for hypothesis to shrink."""
    names = draw(st.lists(st.sampled_from([f"w{i}" for i in range(40)]), unique=True, max_size=30))
    slots = st.integers(1, 12)  # 12 slots for up to 30 names: collisions all the time
    hashes = {n: draw(slots) for n in names}
    recorded = {n: draw(st.integers(-2, 95)) for n in names if draw(st.booleans())}
    pinned = draw(st.dictionaries(st.sampled_from(["p1", "p2", *names[:3]]), slots, max_size=3))
    return sorted(names), recorded, pinned, hashes


@given(_worlds())
def test_allocation_properties(world):
    # Mutations validated red first (each one shrank to a counterexample, then was reverted):
    #   free slots ignore pinned values             → "never a pinned value" fails
    #   a newcomer takes its hash even when held    → "never a pinned value" fails
    #   a loser picks its slot in the claiming pass → "never collided, never moves" fails (shrinks
    #     to two names on slot 1 and a third on 2: the loser took 2, and the third moved)
    #   records ignored (all reallocated each time) → "a record is kept" fails
    names, recorded, pinned, hashes = world
    got = reg.allocate(names, recorded, pinned, _hash(hashes))
    unpinned = [n for n in names if n not in pinned]
    assert set(got) <= set(unpinned)
    assert all(1 <= v <= 89 for v in got.values())
    assert len(set(got.values())) == len(got)  # distinct
    assert not set(got.values()) & set(pinned.values())  # never a pinned value
    assert len(got) == min(len(unpinned), 89 - len(set(pinned.values())))  # all, while room
    # A worktree that never collided never moves: no record, no other name hashing to its slot, and
    # no pin or record holding it — whatever else had to move (an invalid record, one a pin took).
    held = set(pinned.values()) | set(recorded.values())
    for n in unpinned:
        alone = sum(hashes[m] == hashes[n] for m in unpinned) == 1
        if n not in recorded and alone and hashes[n] not in held:
            assert got[n] == hashes[n]
    # A record is kept while it's still its own: in range, no pin on it, nobody before it by name
    # recorded there. (Recomputing everyone from the hashes is "stable" too; this is what isn't.)
    for i, n in enumerate(unpinned):
        mine = recorded.get(n)
        theirs = {recorded.get(m) for m in unpinned[:i]}
        if mine in range(1, 90) and mine not in pinned.values() and mine not in theirs:
            assert got[n] == mine
    assert reg.allocate(names, got, pinned, _hash(hashes)) == got  # stable once recorded


# ── the host's record ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def main(tmp_path):
    m = tmp_path / "repo"
    (m / ".git").mkdir(parents=True)
    return m


def _checkout(main, name):
    path = main.parent / "repo-worktrees" / name
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: elsewhere\n")
    return path


def test_offsets_are_recorded_once_and_freed_on_unregister(main):
    hashes = {"fixes": 48, "supabase": 48}
    for name in hashes:
        reg.register(main, name, _checkout(main, name))
    first = reg.offsets(main, {}, _hash(hashes))
    assert first["fixes"] == 48 and first["supabase"] != 48
    # Recorded: asked again — even with the hash now saying otherwise — the answer holds.
    assert reg.offsets(main, {}, lambda name: 1) == first
    reg.unregister(main, "fixes")
    assert reg.offsets(main, {}, _hash(hashes)) == {"supabase": first["supabase"]}


def test_the_record_sits_beside_the_registry_never_in_it(main):
    # An older supervisor still running after an editable upgrade reads the registry as
    # name → path and drops anything else; offsets in it would read as "nothing registered".
    reg.register(main, "feat", _checkout(main, "feat"))
    reg.offsets(main, {}, lambda name: 7)
    assert json.loads(reg.store_file(main).read_text()) == {
        "feat": str((main.parent / "repo-worktrees" / "feat").resolve())
    }


# ── stack._offset: where every port comes from ────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path, monkeypatch):
    main = tmp_path / "repo"
    (main / ".git").mkdir(parents=True)
    (main / "foldyard.toml").write_text('[project]\nname = "t"\n')
    monkeypatch.setenv("FOLDYARD_REPO", str(main))
    monkeypatch.delenv("WT_OFFSET", raising=False)
    monkeypatch.delenv("FY_WORKTREE_OFFSETS", raising=False)
    config.clear_caches()
    monkeypatch.setattr(stack, "main_repo", lambda: main)
    return main


def test_tangibles_two_worktrees_on_one_hash_get_two_ports(repo, register_worktree):
    # The real pair: both names cksum to 48.
    assert stack._hashed_offset("sec-platform-fixes") == stack._hashed_offset("sec-supabase-url")
    for name in ("sec-platform-fixes", "sec-supabase-url"):
        register_worktree(repo, name, repo.parent / "repo-worktrees" / name)
    assert stack._offset("sec-platform-fixes") != stack._offset("sec-supabase-url")
    assert stack.worktree_offsets() == {
        "sec-platform-fixes": stack._offset("sec-platform-fixes"),
        "sec-supabase-url": stack._offset("sec-supabase-url"),
    }


def test_an_unregistered_worktree_keeps_its_hash(repo):
    assert stack._offset("feat") == stack._hashed_offset("feat")


def test_a_pin_still_wins_and_is_kept_out_of_the_allocation(repo, register_worktree):
    other = "sec-supabase-url"
    (repo / "foldyard.local.toml").write_text(
        f"[worktree-offsets]\npinned = {stack._hashed_offset(other)}\n"
    )
    config.clear_caches()
    register_worktree(repo, other, repo.parent / "repo-worktrees" / other)
    assert stack._offset(other) != stack._hashed_offset(other)  # the pin holds its hashed slot
    assert stack._offset("pinned") == stack._hashed_offset(other)


def test_in_a_box_the_offsets_come_from_what_the_host_baked(repo, monkeypatch):
    # The box can't read the host's record; the host bakes every worktree's offset at create.
    monkeypatch.setenv("IN_DEVBOX", "1")
    monkeypatch.setenv("FY_WORKTREE_OFFSETS", json.dumps({"sec-supabase-url": 3}))
    assert stack._offset("sec-supabase-url") == 3
    assert stack._offset("feat") == stack._hashed_offset("feat")  # added since: the hash
    monkeypatch.setenv("FY_WORKTREE_OFFSETS", "not json")
    assert stack._offset("sec-supabase-url") == stack._hashed_offset("sec-supabase-url")
