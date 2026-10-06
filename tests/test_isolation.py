"""The suite is isolated from where it runs: neither the directory the checkout sits in nor whether
the operator adopted it may change what a test sees. Each test here failed somewhere real first —
the comments say where. The subprocess half of the same promise is ``test_hermetic.py``; the
live modules' half (the isolation must NOT reach them) is ``test_isolation_e2e.py``."""

from __future__ import annotations

import types

from foldyard import config


def test_the_ambient_config_is_the_tree_from_the_first_read():
    # devmode's import-time constants fill `_toml_ambient`'s cache at collection, through the REAL
    # host read, before conftest's seam swaps it — so a test run alone in an unadopted checkout
    # read `{}`, and passed in the full file only because an earlier test cleared the cache.
    assert config._toml() == config._tree_toml(config.repo_root())


def test_a_test_may_resolve_a_scratch_repo(tmp_path, monkeypatch):
    # What test_cli's verb tests do: point FOLDYARD_REPO at a scratch repo, resolve, and leave the
    # env to monkeypatch — the memoized root then outlived it, and test_box_bootstrap_steps_stay_
    # image_agnostic, next on the same worker, read the scratch repo's (empty) config and failed.
    monkeypatch.setenv("FOLDYARD_REPO", str(tmp_path))
    config.clear_caches()
    assert config.repo_root() == tmp_path.resolve()


def test_the_next_test_resolves_the_checkout_the_suite_runs_in():
    # The pair above runs in this order in one process; split across workers this passes anyway.
    cached = config.repo_root()
    config._repo_root_ambient.cache_clear()
    assert cached == config.repo_root()


def _module(name: str, *imported_from: str) -> types.ModuleType:
    module = types.ModuleType(name)
    for i, source in enumerate(imported_from):
        fn = types.FunctionType((lambda: None).__code__, {}, f"helper{i}")
        fn.__module__ = source
        setattr(module, fn.__name__, fn)
    return module


def test_only_a_live_module_running_the_real_cli_reads_the_hosts_state():
    from conftest import _runs_the_real_cli

    # test_e2e's runner, directly or through e2e_host's re-exports: the host tier.
    assert _runs_the_real_cli(_module("test_e2e", "test_e2e"))
    assert _runs_the_real_cli(_module("test_box_e2e", "e2e_host"))
    assert _runs_the_real_cli(_module("test_host_daemons_e2e", "test_e2e"))
    # The in-process proxy/box e2es build their own wiring in tmp — test_proxy_box_e2e grants
    # into the per-test allow store and turns its wall on, which must never be the operator's.
    assert not _runs_the_real_cli(_module("test_proxy_box_e2e", "e2e_box"))
    assert not _runs_the_real_cli(_module("test_proxy_e2e"))
    # The name alone decides nothing: a unit test importing a helper is still a unit test.
    assert not _runs_the_real_cli(_module("test_stack", "e2e_host"))
