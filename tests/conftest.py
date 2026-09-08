"""Shared fixtures. Tests must never touch the real ~/.foldyard state or the repo
mirror, hit GCP/git/network, or depend on where they're run (CI, box, Mac)."""

from __future__ import annotations

import os

import pytest
from hypothesis import HealthCheck, settings

from foldyard import config, devmode, plugins

# ── hypothesis profiles (property-based tests) ──────────────────────────────────────────
# CI must be deterministic (`just foldyard check` never flakes): derandomize replays the same
# example set every run, and no example database means no cross-run state on the runner. Local
# runs keep hypothesis's randomized exploration so new counterexamples accumulate over time.
# function_scoped_fixture is suppressed deliberately: this suite's autouse fixtures
# (isolated_port_registry, isolated_capability_state, …) are per-test ENV PINS, not per-example
# state — property tests that mutate the pinned files reset them per example themselves.
# too_slow is suppressed because it fires spuriously on cold-start (first-draw strategy warm-up
# on a fresh CI runner read as "slow generation") — the strategies here are all small data.
_SUPPRESSED = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]
settings.register_profile("ci", derandomize=True, database=None, suppress_health_check=_SUPPRESSED)
settings.register_profile("dev", suppress_health_check=_SUPPRESSED)
settings.load_profile("ci" if os.environ.get("CI") else "dev")

# ── resolved-config test helpers (per-consumer-registry-plan.md) ───────────────────────
# foldyard's OWN foldyard.toml (the ambient config under `cd foldyard && pytest`) declares NO
# credential tables — it's the generic/spinout exemplar, so the registry under it is core-only.
# The substrate tests, though, exercise the Tangible-SHAPED axis set
# (gcp/storage/github/capture/auth0/llm). Since the registry is now a function of the resolved
# config (Steps B–D), those tests bind a synthetic "full" config — exactly the plan's "build a
# real registry from a synthetic config".

# A full consumer config: declares the gcp-metadata + github + auth0-sim + llm plugin namespaces
# and the proxy table, so the active axes are {gcp, storage, github, capture, auth0, llm} (no
# [[inject]]/[claude].keyless, so no extra axes). The github axis needs its [plugins.github]
# declaration now (self-gated like claude/codex keyless). gcp-metadata.project is set so the gcp axis
# self-gate (Step D) passes; an [[overlay]] wired on `storage=staging` is what makes the storage
# axis appear (config.overlay_when_axes — the gcp plugin's self-gate). The gcp identity overlays
# are declared here too so the overlay-resolution tests see them (there is no built-in default any
# more — overlays are config-only). Deliberately NO auth0/llm overlays: the devmode overlay test
# pins auth0=real precisely to keep the gcp/storage overlays isolated, so wiring an auth0 overlay
# here would change what it asserts.
FULL_TOML = {
    "project": {"name": "tangible", "app_port": "WEB_PORT"},
    "ports": {"WEB_PORT": 3000},
    "proxy": {},
    "plugins": {
        "gcp-metadata": {"project": "acme-staging"},
        "github": {},
        "auth0-sim": {},
        "llm": {},
    },
    "overlay": [
        {"file": "dev-stack/compose.identity.yml", "when": {"gcp": "sa"}},
        {"file": "dev-stack/compose.identity-data.yml", "when": {"gcp": "sa"}},
        {"file": "dev-stack/compose.storage-staging.yml", "when": {"storage": "staging"}},
    ],
    # The wiring-dependent cross-axis requirements ride the consumer config now ([[require]] —
    # the plugins declare none of these): llm=record/live and storage=staging both consume the
    # ADC only gcp=sa (or its superset, user) grants. Mirrors the real foldyard.toml.
    "require": [
        {
            "axis": "llm",
            "when": ["record", "live"],
            "needs": "gcp",
            "accepts": ["sa", "user"],
            "reason": "the runtime-SA identity",
        },
        {
            "axis": "storage",
            "when": "staging",
            "needs": "gcp",
            "accepts": ["sa", "user"],
            "reason": "the runtime-SA identity",
        },
    ],
}

# A generic consumer config: declares none of the credential tables, so the registry is core-only
# (no gcp/storage/auth0/llm/capture axis). The regression guard for the spinout boundary (plan
# Test strategy).
GENERIC_TOML: dict = {"project": {"name": "generic"}}


def make_config(toml: dict, worktree: str = "") -> config.Config:
    """A resolved :class:`~foldyard.config.Config` from an in-memory toml (repo root = the ambient
    one; it's irrelevant to axis membership). Pass to ``Registry(plugins, config=…)`` or bind with
    ``config.using(…)`` to exercise a synthetic consumer's registry."""
    return config.Config(repo_root=config.repo_root(), worktree=worktree, toml=toml)


@pytest.fixture
def full_config_bound():
    """Bind the Tangible-shaped :data:`FULL_TOML` as the active config for the test, so the live
    registry (and ``devmode.axes()`` / mode round-trips) sees the {gcp, storage, github, capture,
    auth0, llm} axis set. Modules whose tests assume those axes opt in via ``pytestmark =
    pytest.mark.usefixtures("full_config_bound")``."""
    plugins._clear_registry_cache()
    with config.using(make_config(FULL_TOML)):
        yield
    plugins._clear_registry_cache()


@pytest.fixture(autouse=True)
def _fresh_registry_cache():
    """The registry is cached per resolved-config CONTENT, but many tests vary the registry by
    monkeypatching config GATING functions (``gcp_metadata_declared``, ``proxy_enabled``, …) rather
    than the toml — invisible to the content key. Clear the cache around every test so one test's
    monkeypatched registry can't leak into the next via a same-content key."""
    plugins._clear_registry_cache()
    yield
    plugins._clear_registry_cache()


@pytest.fixture(autouse=True)
def isolated_port_registry(tmp_path, monkeypatch):
    """Point the cross-project port-band registry (ports.py) at a per-test file, so no test ever
    reads or writes the real ~/.foldyard/ports.json — and every test's project deterministically
    allocates the FIRST band (proxy base 41000, minter base 41100) in its own empty registry."""
    monkeypatch.setenv("FY_PORTS_FILE", str(tmp_path / "fy-ports.json"))


@pytest.fixture(autouse=True)
def isolated_allow_store(tmp_path, monkeypatch):
    """Point the egress allow-store + the effective file the proxy reads at per-test paths, so no
    test reads or writes the real ~/.foldyard/<project>/allow-store.json — and every test starts
    from "nothing granted, nothing declined".

    The same local/CI divergence the fixtures above exist for: a developer's store carries their
    real grants and their `fy allow wall` answer, CI's carries nothing, so a test that reads the
    ambient store asserts a different wall in each place. (An empty store still falls back to
    ``[proxy] default_deny`` — the repo seed — so a test whose SUBJECT is the wall must state its
    posture itself: `allowlist.grant(...)` / `allowlist.set_wall(...)` into this isolated store.)"""
    monkeypatch.setenv("FOLDYARD_ALLOW_STORE", str(tmp_path / "allow-store.json"))
    monkeypatch.setenv("FOLDYARD_ALLOW_FILE", str(tmp_path / "allow-effective.json"))


@pytest.fixture(autouse=True)
def isolated_capability_state(tmp_path, monkeypatch):
    """Point the supervisor's capability-probe results file at a per-test path and pin the test
    clock to real time, so no test reads or writes the real ~/.foldyard capabilities.json — or
    picks up a developer's live `fy clock` skew (FOLDYARD_CLOCK_OFFSET=0 short-circuits the
    offset-file read). Also resets the supervisor's per-process probe cache."""
    from foldyard import supervisor

    monkeypatch.setenv("FOLDYARD_CAPABILITIES_FILE", str(tmp_path / "capabilities.json"))
    monkeypatch.setenv("FOLDYARD_CLOCK_OFFSET", "0")
    supervisor._probe_state.clear()
    yield
    supervisor._probe_state.clear()


@pytest.fixture(autouse=True)
def isolated_config_pin(tmp_path, monkeypatch):
    """Point the ADOPTED-config snapshot (configpin) at a per-test dir, so no test reads or writes
    the real ``~/.foldyard/<project>/*/config/`` — and every test starts from "nothing adopted
    yet", which falls back to the working tree (i.e. the pre-pinning behaviour every other test
    was written against). Pin tests build their own dirs and monkeypatch this back.

    Also clears the supervisor's per-process drift-report memory, like ``isolated_capability_state``
    does for the probe cache: it's keyed by worktree, so one test's drift would silence the next
    test's report."""
    from foldyard import configpin, supervisor

    pins = tmp_path / "config-pin"
    monkeypatch.setattr(
        configpin, "pin_dir", lambda cfg: pins / (getattr(cfg, "worktree", "") or "main")
    )
    # The pre-move location too: it resolves through `config.posture_dir()` → the REAL
    # ~/.foldyard/<project>/, so a test writing one would both escape tmp_path and leak an
    # "already adopted" state into every later test (it did, once).
    monkeypatch.setattr(
        configpin,
        "_legacy_pin_dir",
        lambda cfg: pins / "legacy" / (getattr(cfg, "worktree", "") or "main"),
    )
    supervisor._config_drift_seen.clear()
    yield
    supervisor._config_drift_seen.clear()


@pytest.fixture(autouse=True)
def scrubbed_box_session_env(monkeypatch):
    """Strip the ambient dev-box session env so the suite behaves identically on a Mac, in CI,
    and INSIDE a foldyard box. A box session exports IN_DEVBOX=1 (flips ``config.in_box()`` onto
    the in-box code paths) and the pinned daemon ports/proxy (``FY_PROXY_PORT``/
    ``GCP_MINTER_PORT``/``FY_PROXY`` — the project's REAL band, e.g. 8088), which shadow the
    per-test port registry above and failed 10 Mac-path tests when run in-box. A test that means
    "in the box" or "port pinned" sets these explicitly (setenv / monkeypatching ``in_box``).

    WORKTREE is scrubbed for the same reason: a WORKTREE box (or `WORKTREE=… fy …`) exports it, and
    ``config.worktree_suffix()``/``active_worktree()`` read it — so the daemon names become
    ``egress-proxy@<wt>`` etc. and every test asserting the bare main-checkout name fails ONLY when
    the suite runs inside a worktree. Clearing it pins the suite to the main checkout; the worktree
    tests setenv it themselves (autouse runs first, so their setenv wins).

    ``*_PROXY``/``*_proxy`` for the same reason, learned the hard way: a box session exports
    HTTPS_PROXY, and ``verify``'s wall section reads it as the wall's permitted path. Two verify
    tests therefore passed in-box on the ambient value and failed in CI, where nothing exports one
    — the divergence this fixture exists to prevent. Scrubbing them makes local runs agree with
    CI; the wall tests set the var themselves. NOT the CA vars beside them (SSL_CERT_FILE,
    REQUESTS_CA_BUNDLE…): the opt-in proxy e2es need a real trust store."""
    for var in (
        "IN_DEVBOX",
        "FY_PROXY_PORT",
        "GCP_MINTER_PORT",
        "FY_PROXY",
        "WORKTREE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def deterministic_engine(monkeypatch):
    """Pin the container engine so golden command tests assert one engine regardless of
    whether the host (CI / box / Mac) has podman or docker on PATH. Engine-detection
    tests override this by deleting FOLDYARD_ENGINE themselves (via fresh_config)."""
    monkeypatch.setenv("FOLDYARD_ENGINE", "podman")


@pytest.fixture
def fresh_config(monkeypatch):
    """Give config a clean slate: clears the lru_caches and returns a `set(**env)`
    helper that sets FOLDYARD_* env vars and re-clears the caches so the next lookup
    re-resolves. Caches are cleared again on teardown so no state leaks between tests."""

    def reset():
        config.clear_caches()

    def setenv(**env):
        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, str(value))
        reset()

    reset()
    yield setenv
    reset()


@pytest.fixture
def isolated_state(tmp_path, monkeypatch, full_config_bound):
    """Point the host state files at a tmp dir and pretend we're on the Mac, so
    set_mode/read/write_mirror round-trip without touching ~/.foldyard or the repo. Binds the
    full config (via ``full_config_bound``) so the Tangible-shaped axes are active. The state
    paths are now read live from ``config`` (no devmode snapshots), so we patch THOSE. Returns
    the tmp paths."""
    auth = tmp_path / "dev-mode.json"
    mirror = tmp_path / "mirror.json"
    host_env = tmp_path / "host.env"
    monkeypatch.setattr(config, "mode_file", lambda: auth)
    monkeypatch.setattr(config, "mirror_file", lambda: mirror)
    monkeypatch.setattr(config, "host_env_file", lambda: host_env)
    monkeypatch.setattr(devmode, "in_box", lambda: False)
    monkeypatch.setenv("FOLDYARD_STATE_DIR", str(tmp_path))  # log_dir/state_dir → tmp
    return {"auth": auth, "mirror": mirror, "host_env": host_env, "dir": tmp_path}
