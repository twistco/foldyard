"""A port pool: one ``[ports]`` key whose worktrees take their port from a fixed list.

A worktree's offset moves its whole port band, and that was the only way to choose its ports.
Tangible's staging Auth0 accepts callbacks on ``localhost:3000``–``3005`` only, so a worktree on
its hashed offset (``3042``) couldn't log in, and moving the offset after the fact moved every port,
both host daemon ports and what the box was created with (2026-11-19). A pooled key leaves the band:
``fy worktree add`` gives the new worktree the lowest free port of the pool, recorded by the host,
and refuses when none is free. Worktrees registered before the pool keep their band port."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from foldyard import config, stack
from foldyard import worktree_registry as reg

# ── the config ────────────────────────────────────────────────────────────────────────────────


def _toml(fresh_config, tmp_path, ports: str) -> None:
    (tmp_path / "foldyard.toml").write_text(f"[ports]\n{ports}\n")
    fresh_config(FOLDYARD_REPO=tmp_path)


def test_a_pooled_key_keeps_its_base_beside_plain_ones(fresh_config, tmp_path):
    _toml(
        fresh_config,
        tmp_path,
        'APP_PORT = { base = 3000, worktrees = "3001-3005" }\nPG_PORT = 5533',
    )
    assert config.port_bases() == {"APP_PORT": 3000, "PG_PORT": 5533}
    assert config.port_pools() == {"APP_PORT": range(3001, 3006)}
    assert config.port_pool_problems() == []


def test_a_table_without_a_pool_is_a_plain_base(fresh_config, tmp_path):
    _toml(fresh_config, tmp_path, "APP_PORT = { base = 3000 }")
    assert config.port_bases() == {"APP_PORT": 3000}
    assert config.port_pools() == {}


@pytest.mark.parametrize(
    "ports",
    [
        'APP_PORT = { base = 3000, worktrees = "3005-3001" }',  # backwards
        'APP_PORT = { base = 3000, worktrees = "3001" }',  # not a range
        'APP_PORT = { base = 3000, worktrees = "a-b" }',
        "APP_PORT = { base = 3000, worktrees = [3001, 3005] }",
        'APP_PORT = { base = 3000, worktrees = "0-5" }',  # not a port
        'APP_PORT = { base = 3000, worktrees = "65530-65540" }',
    ],
)
def test_a_pool_that_isnt_a_range_of_ports_is_a_problem_not_a_pool(fresh_config, tmp_path, ports):
    _toml(fresh_config, tmp_path, ports)
    assert config.port_pools() == {}
    assert len(config.port_pool_problems()) == 1
    assert "APP_PORT" in config.port_pool_problems()[0]


def test_a_pool_inside_another_keys_band_is_a_problem(fresh_config, tmp_path):
    # E2E_PORT's worktrees publish on 3011..3099: a pool there would hand one of them out twice.
    _toml(
        fresh_config,
        tmp_path,
        'APP_PORT = { base = 3000, worktrees = "3008-3012" }\nE2E_PORT = 3010',
    )
    assert config.port_pools() == {}
    [problem] = config.port_pool_problems()
    assert "APP_PORT" in problem and "E2E_PORT" in problem


def test_two_pools_that_overlap_are_a_problem(fresh_config, tmp_path):
    _toml(
        fresh_config,
        tmp_path,
        'A = { base = 1000, worktrees = "4001-4005" }\nB = { base = 2000, worktrees = "4005-4009" }',
    )
    assert config.port_pools() == {}
    assert config.port_pool_problems()


def test_its_own_band_is_no_problem(fresh_config, tmp_path):
    # Tangible's pool is inside APP_PORT's own band: the worktrees still on that band hold their
    # ports (see the allocation), so it's allowed.
    _toml(fresh_config, tmp_path, 'APP_PORT = { base = 3000, worktrees = "3001-3005" }')
    assert config.port_pool_problems() == []


# ── picking a port (pure) ─────────────────────────────────────────────────────────────────────


def test_the_lowest_free_port_of_each_pool():
    pools = {"APP_PORT": range(3001, 3006)}
    assert reg.pick_ports(pools, {"APP_PORT": {3001, 3003}}, {}) == {"APP_PORT": 3002}


def test_a_full_pool_picks_nothing_for_any_key():
    pools = {"A": range(1, 3), "B": range(10, 12)}
    assert reg.pick_ports(pools, {"B": {10, 11}}, {}) is None


def test_a_port_already_its_own_is_kept():
    pools = {"APP_PORT": range(3001, 3006)}
    assert reg.pick_ports(pools, {"APP_PORT": {3001}}, {"APP_PORT": 3004}) == {"APP_PORT": 3004}
    # …unless someone else holds it, or it has left the pool.
    assert reg.pick_ports(pools, {"APP_PORT": {3004}}, {"APP_PORT": 3004}) == {"APP_PORT": 3001}
    assert reg.pick_ports(pools, {}, {"APP_PORT": 3042}) == {"APP_PORT": 3001}


@st.composite
def _pool_worlds(draw):
    keys = draw(st.lists(st.sampled_from(["A", "B", "C"]), unique=True, min_size=1))
    pools = {k: range(100 * i, 100 * i + draw(st.integers(1, 6))) for i, k in enumerate(keys)}
    taken = {
        k: set(draw(st.lists(st.integers(100 * i - 1, 100 * i + 7)))) for i, k in enumerate(keys)
    }
    mine = {
        k: draw(st.integers(100 * i - 1, 100 * i + 7))
        for i, k in enumerate(keys)
        if draw(st.booleans())
    }
    return pools, taken, mine


@given(_pool_worlds())
def test_picking_properties(world):
    # Mutations validated red first (each shrank to a counterexample, then was reverted):
    #   a kept port ignores `taken`               → "never a taken port" fails
    #   a full key is skipped, not a refusal       → "nothing iff some pool is full" fails
    #   the free port is the highest, not lowest   → "the lowest free" fails
    pools, taken, mine = world
    got = reg.pick_ports(pools, taken, mine)
    full = any(set(pool) <= taken.get(k, set()) for k, pool in pools.items())
    assert (got is None) == full  # nothing iff some pool is full: all keys or none
    if got is None:
        return
    assert set(got) == set(pools)
    for key, port in got.items():
        assert port in pools[key]  # always in its pool
        assert port not in taken.get(key, set())  # never a taken port
        if mine.get(key) in pools[key] and mine[key] not in taken.get(key, set()):
            assert port == mine[key]  # its own, while it's still free
        else:
            assert port == min(p for p in pools[key] if p not in taken.get(key, set()))


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
    reg.register(main, name, path)
    return path


POOL = {"APP_PORT": range(3001, 3004)}


def test_a_claim_is_recorded_and_freed_when_the_worktree_goes(main):
    _checkout(main, "a")
    _checkout(main, "b")
    assert reg.claim_ports(main, "a", POOL, {}) == {"APP_PORT": 3001}
    assert reg.claim_ports(main, "b", POOL, {}) == {"APP_PORT": 3002}
    assert reg.claim_ports(main, "a", POOL, {}) == {"APP_PORT": 3001}  # asked again: the same
    assert reg.recorded_ports(main) == {"a": {"APP_PORT": 3001}, "b": {"APP_PORT": 3002}}
    reg.unregister(main, "a")
    assert reg.recorded_ports(main) == {"b": {"APP_PORT": 3002}}
    _checkout(main, "c")
    assert reg.claim_ports(main, "c", POOL, {}) == {"APP_PORT": 3001}  # a's port, free again


def test_a_full_pool_claims_nothing_and_records_nothing(main):
    for name in ("a", "b", "c", "d"):
        _checkout(main, name)
    for name in ("a", "b", "c"):
        assert reg.claim_ports(main, name, POOL, {})
    before = reg.recorded_ports(main)
    assert reg.claim_ports(main, "d", POOL, {}) is None
    assert reg.recorded_ports(main) == before


def test_ports_held_on_the_band_are_never_claimed(main):
    # A worktree registered before the pool still publishes on its band, inside the pool.
    _checkout(main, "new")
    assert reg.claim_ports(main, "new", POOL, {"APP_PORT": {3001}}) == {"APP_PORT": 3002}


def test_the_record_sits_beside_the_registry_never_in_it(main):
    path = _checkout(main, "feat")
    reg.claim_ports(main, "feat", POOL, {})
    assert json.loads(reg.store_file(main).read_text()) == {"feat": str(path.resolve())}
    assert reg.ports_file(main).parent == reg.store_file(main).parent


# ── stack: the port a worktree's stack publishes ──────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path, monkeypatch):
    main = tmp_path / "repo"
    (main / ".git").mkdir(parents=True)
    (main / "foldyard.toml").write_text(
        '[project]\nname = "t"\n[ports]\n'
        'APP_PORT = { base = 3000, worktrees = "3001-3003" }\nPG_PORT = 5533\n'
    )
    monkeypatch.setenv("FOLDYARD_REPO", str(main))
    for var in ("WT_OFFSET", "FY_WORKTREE_OFFSETS", "FY_WORKTREE_PORTS", "IN_DEVBOX"):
        monkeypatch.delenv(var, raising=False)
    config.clear_caches()
    monkeypatch.setattr(stack, "main_repo", lambda: main)
    # Every band far from the pool, so a name that happens to hash to 1..3 can't hold a pool port.
    monkeypatch.setattr(stack, "_hashed_offset", lambda name: 50)
    return main


def _add(repo, name):
    path = repo.parent / "repo-worktrees" / name
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: test\n")
    reg.register(repo, name, path)


def test_main_publishes_on_the_base(repo):
    assert stack.worktree_port("APP_PORT", "") == 3000


def test_a_worktree_with_a_claim_publishes_on_it_and_the_rest_on_its_band(repo):
    _add(repo, "feat")
    assert stack.claim_ports("feat") == {"APP_PORT": 3001}
    assert stack.worktree_port("APP_PORT", "feat") == 3001
    assert stack.worktree_port("PG_PORT", "feat") == 5533 + stack._offset("feat")


def test_a_worktree_from_before_the_pool_keeps_its_band_and_holds_it(repo, monkeypatch):
    monkeypatch.setattr(stack, "_hashed_offset", lambda name: 1 if name == "old" else 50)
    _add(repo, "old")  # recorded on offset 1: its band port is 3001, inside the pool
    assert stack.worktree_port("APP_PORT", "old") == 3001
    _add(repo, "new")
    assert stack.claim_ports("new") == {"APP_PORT": 3002}  # 3001 is old's, on its band


def test_a_full_pool_names_who_holds_each_port(repo):
    for name in ("a", "b", "c", "d"):
        _add(repo, name)
    for name in ("a", "b", "c"):
        stack.claim_ports(name)
    assert stack.claim_ports("d") is None
    assert stack.pool_holders() == {"APP_PORT": {3001: "a", 3002: "b", 3003: "c"}}


def test_the_box_is_given_every_worktrees_pooled_ports(repo, monkeypatch):
    monkeypatch.setattr(stack, "_offset", lambda name: 40)
    _add(repo, "feat")
    _add(repo, "old")
    stack.claim_ports("feat")
    assert stack.worktree_ports() == {"feat": {"APP_PORT": 3001}, "old": {"APP_PORT": 3040}}


def test_in_a_box_the_port_comes_from_what_the_host_baked(repo, monkeypatch):
    monkeypatch.setenv("IN_DEVBOX", "1")
    monkeypatch.setenv("FY_WORKTREE_PORTS", json.dumps({"feat": {"APP_PORT": 3002}, "old": {}}))
    assert stack.worktree_port("APP_PORT", "feat") == 3002
    assert stack.worktree_port("APP_PORT", "old") == 3000 + stack._offset("old")  # no pool then


def test_in_a_box_a_worktree_added_since_is_refused_not_guessed(repo, monkeypatch, capsys):
    # Its band port would be outside the pool: the very failure the pool exists to prevent.
    monkeypatch.setenv("IN_DEVBOX", "1")
    monkeypatch.setenv("FY_WORKTREE_PORTS", json.dumps({"feat": {"APP_PORT": 3002}}))
    with pytest.raises(stack.PortsUnknown):
        stack.worktree_port("APP_PORT", "later")
    with pytest.raises(SystemExit):  # the stack verbs say so and stop
        stack._published_ports("later")
    assert "fy box recreate" in capsys.readouterr().err


def test_a_box_from_before_pools_keeps_the_band(repo, monkeypatch):
    monkeypatch.setenv("IN_DEVBOX", "1")  # no FY_WORKTREE_PORTS at all: an older foldyard made it
    assert stack.worktree_port("APP_PORT", "feat") == 3000 + stack._offset("feat")


def test_the_stack_is_given_the_pooled_port_and_the_band_for_the_rest(repo):
    _add(repo, "feat")
    stack.claim_ports("feat")
    off = stack._offset("feat")
    assert stack._published_ports("feat") == {"APP_PORT": "3001", "PG_PORT": str(5533 + off)}
    assert stack._published_ports("") == {"APP_PORT": "3000", "PG_PORT": "5533"}


def test_the_workspaces_card_shows_the_pooled_port(repo, monkeypatch):
    from foldyard import devmode

    (repo / "foldyard.toml").write_text(
        (repo / "foldyard.toml")
        .read_text()
        .replace('name = "t"', 'name = "t"\napp_port = "APP_PORT"')
    )
    config.clear_caches()
    monkeypatch.setattr(devmode, "main_repo", lambda: repo)
    _add(repo, "feat")
    wt = repo.parent / "repo-worktrees" / "feat"
    (wt / "foldyard.toml").write_text((repo / "foldyard.toml").read_text())  # its branch's copy
    # Its adopted config, as the host reads it (adoption itself is test_configpin's).
    monkeypatch.setattr(devmode, "worktree_config", lambda w: config.resolve(worktree=w, repo=wt))
    stack.claim_ports("feat")
    item: dict = {}
    devmode._describe_checkout(item, "feat")
    assert item["app_port"] == 3001


# ── fy doctor ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def doctor_repo(repo, monkeypatch):
    from foldyard import devmode

    monkeypatch.setattr(devmode, "main_repo", lambda: repo)
    return repo


def test_doctor_counts_the_pool(doctor_repo):
    from foldyard import devmode

    _add(doctor_repo, "feat")
    stack.claim_ports("feat")
    assert list(devmode._port_pool_check()) == [("ok", "ports pool", "APP_PORT: 1 of 3 taken")]


def test_doctor_names_a_worktree_still_on_its_band_and_the_fix(doctor_repo):
    from foldyard import devmode

    _add(doctor_repo, "old")
    [(status, name, detail)] = devmode._port_pool_check()
    assert (status, name) == ("warn", "ports pool")
    assert "old (APP_PORT 3050)" in detail and "`fy worktree add old`" in detail


def test_doctor_fails_a_misconfigured_pool(doctor_repo):
    from foldyard import devmode

    (doctor_repo / "foldyard.toml").write_text(
        '[project]\nname = "t"\n[ports]\nAPP_PORT = { base = 3000, worktrees = "x" }\n'
    )
    config.clear_caches()
    [(status, _, detail)] = devmode._port_pool_check()
    assert status == "fail" and "APP_PORT" in detail


def test_doctor_says_nothing_without_a_pool(doctor_repo):
    from foldyard import devmode

    (doctor_repo / "foldyard.toml").write_text('[project]\nname = "t"\n[ports]\nAPP_PORT = 3000\n')
    config.clear_caches()
    assert list(devmode._port_pool_check()) == []


def test_a_box_made_before_a_pool_refuses_a_worktree_added_since(repo, monkeypatch, capsys):
    # The box is made with no pool (so every worktree it knows has no pooled ports), then a pool is
    # adopted and a worktree added. Reading "nothing baked" there as "an older foldyard made it"
    # published the new worktree on its offset port: the failure the pool exists for.
    (repo / "foldyard.toml").write_text('[project]\nname = "t"\n[ports]\nAPP_PORT = 3000\n')
    config.clear_caches()
    _add(repo, "old")
    baked = json.dumps(stack.worktree_ports())
    assert json.loads(baked) == {"old": {}}
    monkeypatch.setenv("IN_DEVBOX", "1")
    monkeypatch.setenv("FY_WORKTREE_PORTS", baked)
    (repo / "foldyard.toml").write_text(
        '[project]\nname = "t"\n[ports]\nAPP_PORT = { base = 3000, worktrees = "3001-3003" }\n'
    )
    config.clear_caches()
    assert stack.worktree_port("APP_PORT", "old") == 3050  # known, no pool then: its offset port
    with pytest.raises(SystemExit):
        stack._published_ports("feat")
    assert "fy box recreate" in capsys.readouterr().err


def test_wt_offset_never_speaks_for_the_other_worktrees_held_ports(repo, monkeypatch):
    # WT_OFFSET is one invocation's override; read for every worktree it made "old" (recorded on
    # offset 50, port 3050) look like it held 3001.
    _add(repo, "old")
    _add(repo, "new")
    monkeypatch.setenv("WT_OFFSET", "1")
    assert stack.claim_ports("new") == {"APP_PORT": 3001}


# ── review round (CodeRabbit, #202) ──────────────────────────────────────────────────────────


def test_a_pool_without_a_base_is_a_problem(fresh_config, tmp_path):
    _toml(fresh_config, tmp_path, 'APP_PORT = { worktrees = "3001-3005" }')
    assert config.port_pools() == {}
    [problem] = config.port_pool_problems()
    assert "APP_PORT" in problem and "base" in problem


def _set_pool(repo, pool):
    (repo / "foldyard.toml").write_text(
        f'[project]\nname = "t"\n[ports]\nAPP_PORT = {{ base = 3000, worktrees = "{pool}" }}\n'
    )
    config.clear_caches()


def test_a_record_left_outside_a_changed_pool_holds_its_offset_port(repo, monkeypatch):
    monkeypatch.setattr(stack, "_hashed_offset", lambda name: 2 if name == "old" else 50)
    _add(repo, "old")
    _set_pool(repo, "3001-3001")
    assert stack.claim_ports("old") == {"APP_PORT": 3001}
    _set_pool(repo, "3002-3003")  # 3001 left the pool: old publishes on its offset port, 3002
    assert stack.worktree_port("APP_PORT", "old") == 3002
    _add(repo, "new")
    assert stack.claim_ports("new") == {"APP_PORT": 3003}


def test_doctor_names_a_worktree_whose_record_left_the_pool(doctor_repo):
    from foldyard import devmode

    _add(doctor_repo, "old")
    stack.claim_ports("old")  # 3001
    _set_pool(doctor_repo, "3002-3003")
    [(status, _, detail)] = devmode._port_pool_check()
    assert status == "warn" and "`fy worktree add old`" in detail


def test_in_a_box_the_card_of_a_worktree_added_since_shows_no_port(repo, monkeypatch):
    # The refusal belongs to the stack verbs; listing workspaces must not exit on it.
    from foldyard import devmode

    monkeypatch.setenv("IN_DEVBOX", "1")
    monkeypatch.setenv("FY_WORKTREE_PORTS", json.dumps({"feat": {"APP_PORT": 3002}}))
    (repo / "foldyard.toml").write_text(
        (repo / "foldyard.toml")
        .read_text()
        .replace('name = "t"', 'name = "t"\napp_port = "APP_PORT"')
    )
    config.clear_caches()
    monkeypatch.setattr(devmode, "worktree_config", lambda w: config.resolve(worktree=w, repo=repo))
    item: dict = {"path": str(repo)}
    devmode._describe_checkout(item, "later")
    assert item["app_port"] is None


def test_a_claim_keeps_a_record_made_while_it_waited_for_the_lock(repo, monkeypatch):
    # B registers and claims between A reading the registry and A taking the lock.
    import fcntl as real_fcntl

    _add(repo, "a")
    pools = {"APP_PORT": range(3001, 3004)}
    lock = real_fcntl.flock
    raced = []

    def flock(fh, op):
        lock(fh, op)
        if not raced and op == real_fcntl.LOCK_EX:
            raced.append(1)
            _add(repo, "b")
            reg.ports_file(repo).write_text(json.dumps({"b": {"APP_PORT": 3001}}))

    monkeypatch.setattr(reg.fcntl, "flock", flock)
    assert reg.claim_ports(repo, "a", pools, {}) == {"APP_PORT": 3002}
    assert reg.recorded_ports(repo)["b"] == {"APP_PORT": 3001}


def test_a_reader_never_sees_a_half_written_record(repo, monkeypatch):
    _add(repo, "a")
    _add(repo, "b")
    pools = {"APP_PORT": range(3001, 3004)}
    reg.claim_ports(repo, "a", pools, {})
    before = reg.ports_file(repo).read_text()

    def dump(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(reg.json, "dump", dump)
    monkeypatch.setattr(reg.json, "dumps", dump)
    with pytest.raises(OSError):
        reg.claim_ports(repo, "b", pools, {})
    assert reg.ports_file(repo).read_text() == before


# ── the project's pools are the main checkout's (0.12.1) ──────────────────────────────────────


@pytest.fixture
def main_and_old_branch(tmp_path, fresh_config, monkeypatch):
    """A real repo whose main has the pool, and a worktree on a branch cut before it: its own
    foldyard.toml declares APP_PORT plain. Both checkouts' configs are 'adopted' (the conftest seam
    reads each tree)."""
    import subprocess

    main, wt = tmp_path / "repo", tmp_path / "repo-worktrees" / "old"
    main.mkdir()

    def git(*a, cwd=main):
        subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    plain = '[project]\nname = "t"\n[ports]\nAPP_PORT = 3000\nPG_PORT = 5533\n'
    (main / "foldyard.toml").write_text(plain)
    git("add", "foldyard.toml")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "before the pool")
    git("worktree", "add", "-q", "-b", "old", str(wt))
    (main / "foldyard.toml").write_text(
        plain.replace("APP_PORT = 3000", 'APP_PORT = { base = 3000, worktrees = "3001-3003" }')
    )
    monkeypatch.setattr(stack, "main_repo", lambda: main.resolve())
    monkeypatch.setattr(stack, "_hashed_offset", lambda name: 41)
    for var in ("WT_OFFSET", "FY_WORKTREE_OFFSETS", "FY_WORKTREE_PORTS", "IN_DEVBOX", "WORKTREE"):
        monkeypatch.delenv(var, raising=False)
    reg.register(main.resolve(), "old", wt.resolve())
    fresh_config(FOLDYARD_REPO=main)
    return main, wt


def test_a_branch_from_before_the_pool_still_gets_its_pooled_port(
    main_and_old_branch, fresh_config
):
    # Live on Tangible (2026-11-19): `WORKTREE=pool-test fy up` from main published 3001, and
    # `fy up` inside the worktree — whose branch's foldyard.toml had no pool yet — 3041.
    _main, wt = main_and_old_branch
    assert stack.claim_ports("old") == {"APP_PORT": 3001}
    assert stack._published_ports("old")["APP_PORT"] == "3001"  # from main
    fresh_config(FOLDYARD_REPO=wt)  # from inside the worktree
    assert config.port_pools() == {"APP_PORT": range(3001, 3004)}
    assert stack._published_ports("old") == {"APP_PORT": "3001", "PG_PORT": "5574"}


def test_a_bound_worktree_config_reads_the_projects_pools(main_and_old_branch):
    # The TUI card and the supervisor resolve each worktree under its own bound config.
    _main, wt = main_and_old_branch
    stack.claim_ports("old")
    with config.using(config.resolve(worktree="old", repo=wt)):
        assert stack.worktree_port("APP_PORT", "old") == 3001


def test_in_a_box_the_pools_are_main_checkouts_tree(main_and_old_branch, fresh_config, monkeypatch):
    _main, wt = main_and_old_branch
    monkeypatch.setenv("IN_DEVBOX", "1")
    monkeypatch.setenv("FY_WORKTREE_PORTS", json.dumps({"old": {"APP_PORT": 3002}}))
    fresh_config(FOLDYARD_REPO=wt)
    assert stack._published_ports("old")["APP_PORT"] == "3002"


def test_outside_git_the_checkouts_own_pools_are_read(fresh_config, tmp_path):
    _toml(fresh_config, tmp_path, 'APP_PORT = { base = 3000, worktrees = "3001-3005" }')
    assert config.port_pools() == {"APP_PORT": range(3001, 3006)}
