"""The suite is isolated from where it runs: neither the directory the checkout sits in nor whether
the operator adopted it may change what a test sees. Each test here failed somewhere real first —
the comments say where. The subprocess half of the same promise is ``test_hermetic.py``."""

from __future__ import annotations

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
