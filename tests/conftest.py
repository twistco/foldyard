"""Shared fixtures. Tests must never touch the real ~/.foldyard state or the repo
mirror, hit GCP/git/network, or depend on where they're run (CI, box, Mac)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

from foldyard import config, devmode, plugins

# The env the session started with, before any fixture: what a live module must still see
# (tests/test_isolation_e2e.py).
SESSION_ENV = dict(os.environ)

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

# A full consumer config: declares the gcp-metadata + auth0-sim + llm plugin namespaces, the proxy
# table and GitHub as two `[[inject]]` kinds (ADR-0031), so the active axes are {gcp, storage,
# github, github-user, auth0, llm} (no [claude].keyless, so no agent axes). `github` is the App
# (off/on); `github-user` the operator's own token, an emergency switch whose `on` expires — the
# old `github=app` / `github=user` levels, each now its own switch. gcp-metadata.project is set
# so the gcp axis self-gate (Step D) passes; an [[overlay]] wired on `storage=staging` is what makes the storage
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
        "auth0-sim": {},
        "llm": {},
    },
    "inject": [
        {
            "switch": "github",
            "kind": "github-app",
            "app_id": "1234567",
            "installation_id": "7654321",
            "box_env": {"GH_TOKEN": "x"},
        },
        {
            "switch": "github-user",
            "kind": "gh-cli",
            "emergency": True,
            "box_env": {"GH_TOKEN": "x"},
        },
    ],
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
            "switch": "llm",
            "when": ["record", "live"],
            "needs": "gcp",
            "accepts": ["sa", "user"],
            "reason": "the runtime-SA identity",
        },
        {
            "switch": "storage",
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
    registry (and ``devmode.axes()`` / mode round-trips) sees the {gcp, storage, github,
    github-user, auth0, llm} axis set. Modules whose tests assume those axes opt in via ``pytestmark =
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
def isolated_port_registry(tmp_path, monkeypatch, request):
    """Point the cross-project port-band registry (ports.py) at a per-test file, so no test ever
    reads or writes the real ~/.foldyard/ports.json — and every test's project deterministically
    allocates the FIRST band (proxy base 41000, minter base 41100) in its own empty registry.

    NOT for the live modules that drive the real CLI (:func:`_runs_the_real_cli`): their CLI
    subprocesses inherit this env, so the example project was handed the first band — and on a
    host with real projects that band (its pinned VM ssh port included) belongs to one of them; a
    restarted example VM then never came up."""
    if _runs_the_real_cli(request.module):
        return
    monkeypatch.setenv("FY_PORTS_FILE", str(tmp_path / "fy-ports.json"))


@pytest.fixture(autouse=True)
def isolated_allow_store(tmp_path, monkeypatch, request):
    """Point the egress allow-store + the effective file the proxy reads at per-test paths, so no
    test reads or writes the real ~/.foldyard/<project>/allow-store.json — and every test starts
    from "nothing granted, nothing declined".

    The same local/CI divergence the fixtures above exist for: a developer's store carries their
    real grants and their `fy allow enforce` answer, CI's carries nothing, so a test that reads the
    ambient store asserts a different wall in each place. (An empty store still falls back to
    ``[proxy] enforce`` — the repo seed — so a test whose SUBJECT is the wall must state its
    posture itself: `allowlist.grant(...)` / `allowlist.set_wall(...)` into this isolated store.)

    NOT for the live modules that drive the real CLI: `fy allow` in one test and the proxy the
    supervisor launched in another would read two stores. The in-process proxy e2es keep it —
    test_proxy_box_e2e grants into it and turns its wall on."""
    if _runs_the_real_cli(request.module):
        return
    monkeypatch.setenv("FOLDYARD_ALLOW_STORE", str(tmp_path / "allow-store.json"))
    monkeypatch.setenv("FOLDYARD_ALLOW_FILE", str(tmp_path / "allow-effective.json"))
    # The build gate's per-build secrets (hashes): a build test must never write the real one.
    monkeypatch.setenv("FOLDYARD_BUILD_TOKENS", str(tmp_path / "build-tokens.json"))


@pytest.fixture(autouse=True)
def isolated_capability_state(tmp_path, monkeypatch, request):
    """Point the supervisor's capability-probe results file at a per-test path and pin the test
    clock to real time, so no test reads or writes the real ~/.foldyard capabilities.json — or
    picks up a developer's live `fy clock` skew (FOLDYARD_CLOCK_OFFSET=0 short-circuits the
    offset-file read). Also resets the supervisor's per-process probe cache. The credential scope
    record (credscope) lives beside it and is isolated the same way, with the supervisor's memory
    of which scopes it has already reported unreadable.

    The env NOT for the live modules that drive the real CLI: the supervisor their first `fy up`
    starts would write one test's file for the rest of the module. (The caches are this process's
    own, so they are reset for them too.)"""
    from foldyard import supervisor

    if not _runs_the_real_cli(request.module):
        monkeypatch.setenv("FOLDYARD_CAPABILITIES_FILE", str(tmp_path / "capabilities.json"))
        monkeypatch.setenv(
            "FOLDYARD_CREDENTIAL_SCOPES_FILE", str(tmp_path / "credential-scopes.json")
        )
        monkeypatch.setenv("FOLDYARD_CLOCK_OFFSET", "0")
    supervisor._probe_state.clear()
    supervisor._scope_unread_seen.clear()
    yield
    supervisor._probe_state.clear()
    supervisor._scope_unread_seen.clear()


@pytest.fixture(autouse=True)
def isolated_proxy_ca(tmp_path, monkeypatch, request):
    """Point the proxy CA, and the confdir it's generated in, at a MISSING file of mitmproxy's
    default name under the test's tmp dir.
    ``proxy.ensure_ca()`` generates a CA when its file is missing, through the allowlisted
    interpreter, so without this a test wrote a real CA private key into the operator's (or a CI
    runner's) ~/.mitmproxy — and a test on a machine that already had one saw it, which is how a
    test passed locally and failed on CI. A test that wants a CA writes one and re-points this.

    NOT for the live modules that drive the real CLI: inherited, ``MITMPROXY_CA`` names a per-test
    file the proxy never signs with — the mismatch ``ensure_ca`` refuses, and the walled VM embeds
    the CA its proxy really uses."""
    if _runs_the_real_cli(request.module):
        return
    from foldyard.plugins import proxy

    monkeypatch.setattr(proxy, "_proxy_confdir", lambda: tmp_path / "mitmproxy")
    monkeypatch.setenv("MITMPROXY_CA", str(tmp_path / "mitmproxy" / "mitmproxy-ca-cert.pem"))


@pytest.fixture(autouse=True)
def isolated_config_pin(tmp_path, monkeypatch):
    """Point the ADOPTED-config snapshot (configpin) at a per-test dir, so no test reads or writes
    the real ``~/.foldyard/<project>/*/config/`` — and every test starts from "nothing adopted
    yet", which falls back to the working tree (i.e. the pre-pinning behaviour every other test
    was written against). Pin tests build their own dirs and monkeypatch this back.

    Also clears the supervisor's per-process drift-report memory, like ``isolated_capability_state``
    does for the probe cache: it's keyed by worktree, so one test's drift would silence the next
    test's report.

    Kept for the live modules: it reaches no subprocess (their CLI adopts into the real pin dir),
    and an in-process read with nothing adopted here falls back to the same tree they adopted."""
    from foldyard import configpin, supervisor

    pins = tmp_path / "config-pin"
    monkeypatch.setattr(
        configpin, "pin_dir", lambda cfg: pins / (getattr(cfg, "worktree", "") or "main")
    )
    supervisor._config_drift_seen.clear()
    supervisor._held_cursor.clear()
    supervisor._held_notified.clear()
    supervisor._inject_overlaps_seen.clear()
    yield
    supervisor._config_drift_seen.clear()
    supervisor._held_cursor.clear()
    supervisor._held_notified.clear()
    supervisor._inject_overlaps_seen.clear()


@pytest.fixture(autouse=True)
def ambient_reads_the_tree(monkeypatch):
    """On the host the AMBIENT config is the adopted snapshot (``config._host_toml``), and a test
    starts with nothing adopted — so without this every ambient read in the suite would be empty.
    The suite was written against the working tree, so that is what the seam returns here (as if
    each checkout had adopted exactly its tree). ``tests/test_ambient_adopted.py`` runs the real
    host read.

    The ambient resolution is memoized, and the first one happens at COLLECTION — devmode's
    import-time constants — through the real host read, before any fixture: a test run alone in an
    unadopted checkout read ``{}``. So it is dropped on both sides of the swap, the root with the
    parse read from it: the test's first read goes through the seam, from the checkout the suite
    runs in, and nothing a test resolved (a scratch repo it pointed ``FOLDYARD_REPO`` at) outlives it.

    Kept for the live modules: it changes no env (their CLI subprocesses read the real adoption,
    which they make of the example copy's tree), and their in-process probes read that same tree."""
    from foldyard import config

    monkeypatch.setattr(config, "_host_toml", config._tree_toml)
    config.clear_caches()
    yield
    # Only the ambient pair here: a test's own monkeypatches (worktree_offset, main_repo as a plain
    # lambda) are still applied at this teardown, and those have no cache to clear.
    config._repo_root_ambient.cache_clear()
    config._toml_ambient.cache_clear()


@pytest.fixture(autouse=True)
def isolated_worktree_registry(tmp_path, monkeypatch, request):
    """Point the host's worktree registry at a per-test dir: no test reads or writes the real
    ``~/.foldyard/worktrees/``, and every test starts with NO worktree registered. A test that
    wants the host to see a worktree registers it (:func:`register_worktree`).

    NOT for the live modules that drive the real CLI: their CLI subprocesses inherit this env, and
    a per-TEST dir loses what `fy worktree add` registered in one test before the next test's
    `fy up`."""
    if _runs_the_real_cli(request.module):
        return
    monkeypatch.setenv("FOLDYARD_WORKTREE_REGISTRY", str(tmp_path / "worktree-registry"))


@pytest.fixture(autouse=True)
def isolated_worktrees_root(tmp_path, monkeypatch, request):
    """Point the worktrees root at a per-test dir that does not exist. Left to resolve, it is the
    REAL ``<main checkout>-worktrees`` beside the repo the suite runs from — so a checkout standing
    inside it (``~/workspace/foldyard-worktrees/<name>``) had ``current_workspace()`` infer a
    worktree from the CWD, and ``workspaces()`` listed whatever else lives there: 16 TUI tests
    failed in that one place and passed from a copy anywhere else. A test about worktrees builds
    its own root (setenv, or monkeypatching ``config.worktrees_root``).

    NOT for the live modules that drive the real CLI: the VM mounts the worktrees root, so a
    per-test one would change its provisioning under every test."""
    if _runs_the_real_cli(request.module):
        return
    monkeypatch.setenv("FOLDYARD_WORKTREES_ROOT", str(tmp_path / "worktrees"))


@pytest.fixture
def register_worktree():
    """``register_worktree(main, name, path)`` — record a worktree the way `fy worktree add` does
    (creating a stand-in ``.git`` if the test built a bare dir)."""
    from foldyard import worktree_registry

    def _register(main, name, path):
        if not (path / ".git").exists():
            path.mkdir(parents=True, exist_ok=True)
            (path / ".git").write_text("gitdir: test\n")
        worktree_registry.register(main, name, path)

    return _register


@pytest.fixture(autouse=True)
def isolated_podman_desktop(tmp_path, monkeypatch):
    """Point Podman Desktop's settings file at a per-test path that does not exist, so "is Podman
    Desktop installed?" reads NO everywhere — as on a CI runner. The machine follows Podman
    Desktop by default when its settings file exists, so on a developer's Mac every
    ``machine.ensure`` test would otherwise pin a VM's ssh port and register a connection. The
    Podman Desktop tests write the file themselves."""
    from foldyard import podman_desktop

    path = tmp_path / "podman-desktop-settings.json"
    monkeypatch.setattr(podman_desktop, "settings_path", lambda: path)


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
    REQUESTS_CA_BUNDLE…): the opt-in proxy e2es need a real trust store.

    ``FOLDYARD_PODMAN_DESKTOP`` because an operator's explicit ``=1`` would win over the
    isolated "not installed" answer (``isolated_podman_desktop``) and have every
    ``machine.ensure`` test pin a VM's ssh port and register a connection.

    Kept for the live modules: these describe the SHELL the suite was started from (a box session,
    a worktree), not host state, and a live CLI run from a worktree's shell must still act on the
    example copy's main checkout. What a live test needs back it re-adds by name — test_e2e's
    runner restores IN_DEVBOX from the value it read at import."""
    for var in (
        "FOLDYARD_PODMAN_DESKTOP",
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


# ── hermetic subprocess ─────────────────────────────────────────────────────────────────
# The suite must not execute a real host tool. Twice now a code path escaped its mocks at a layer
# below the one the test stubbed: an engine probe that `podman rm -f`'d the box's live dev
# stack (2026-08, hence the dead-socket rule in CLAUDE.md), and the supervisor's "newly blocked
# daemon" push that posted "egress proxy (claude keyless) not started" to a developer's
# Notification Center (2026-09-17). Before this guard a census found 210 tests spawning a real
# process — `limactl` 114, `podman` 111, `gcloud`/`gh` 3 each; `set_mode` round trips were
# running `limactl list` + `podman ps` against the live machine on the way — all passing in CI
# only because those binaries are absent there: local and CI were passing for different reasons.
# Two layers close it, both exempting the opt-in e2e modules (`*_e2e.py`, live by definition):
#   1. `hermetic_path` — PATH is reduced to a shim dir holding only the tools tests legitimately
#      spawn (git for tmp repos, `cksum` for stack._offset parity, the shell + interpreter), so a
#      host tool resolves to "not found" here exactly as on a CI runner, and the code under test
#      takes its "engine unreachable" branch — the hermetic outcome, not a stub of it.
#   2. `no_host_tool_spawn` — the scrub can't see an ABSOLUTE path (`/usr/bin/osascript`,
#      `compose_provider_path()`, anything under /opt/homebrew) or a caller's own `env["PATH"]`,
#      so `subprocess.run`/`Popen`/… resolve what they are about to execute and refuse anything
#      that isn't an allowlisted tool (by name, wherever it lives) or the interpreter, failing
#      the test BY NAME with a BaseException the
#      code's `except OSError`/`except Exception` can't swallow (a swallowed leak reading as a
#      handled error is how both incidents passed green). A command that resolves to nothing is
#      let through: that is the "not installed" path, not a leak. A test that means to run
#      something else says so: `@pytest.mark.spawns("/abs/path/tool")` (registered in pyproject).
#      The shells are allowlisted for what they ARE (`bash -n` parses a script), not for what
#      they run: `bash -c …` / `sh script` carry a whole program past argv[0] — shell=True by
#      another door — so those forms are refused unless the test names the shell
#      (`@pytest.mark.spawns("bash")`). `env` is deliberately absent: it is a wrapper too, and
#      nothing under test spawns it.
# Neither is an env var to opt out of: a test that needs a real host tool is an e2e test.

SPAWNABLE = ("git", "cksum", "bash", "sh", "true", "false", "echo")


SHELLS = ("bash", "sh")


def _is_e2e(request: pytest.FixtureRequest) -> bool:
    return request.node.fspath.basename.endswith("_e2e.py")


# Where test_e2e's CLI runner lives, and the host-tier substrate that re-exports it.
CLI_RUNNERS = ("test_e2e", "e2e_host")


def _runs_the_real_cli(module: object) -> bool:
    """Whether ``module`` is a live module that drives the REAL CLI as a subprocess — test_e2e,
    or any ``*_e2e.py`` built on its runner (every host-tier module imports from e2e_host). Its CLI
    subprocesses, and the supervisor the first `fy up` leaves running for the rest of the session,
    inherit the test's env, so the per-test state fixtures above withhold theirs here: the live
    test reads the host's real state, as the operator's `fy` does.

    Narrower than :func:`_is_e2e` on purpose: the in-process proxy/box e2es build their own wiring
    in tmp and keep every isolation (test_proxy_box_e2e grants into the per-test allow store)."""
    name = getattr(module, "__name__", "").rpartition(".")[2]
    return name.endswith("_e2e") and any(
        getattr(value, "__module__", None) in CLI_RUNNERS for value in vars(module).values()
    )


def _is_box_git_shim(path: str) -> bool:
    """Whether ``path`` is foldyard's own in-box git shim (``assets/box/git-index-shim.sh``,
    installed to /usr/local/bin/git by the box bootstrap)."""
    try:
        with open(path, "rb") as f:
            return b"foldyard git shim" in f.read(512)
    except OSError:
        return False


def _spawnable(tool: str) -> str | None:
    """The real ``tool`` on the ambient PATH — for git, PAST the box's shim. The shim finds the
    git it wraps by searching PATH for a `git` that isn't itself, and the shim dir below holds
    only the symlink back to it: every git a test ran in a dev box exited 127 ("no real git on
    PATH") — 100-odd failures that CI, which has no shim, never saw. The tests' git works on
    tmp repos, where the shim's per-kernel index split has nothing to protect; the shim's own
    tests (test_git_shim.py) run it deliberately, over this real git."""
    if tool != "git":
        return shutil.which(tool)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        found = shutil.which(tool, path=d)
        if found and not _is_box_git_shim(found):
            return found
    return None


@pytest.fixture(scope="session")
def _shim_bin(tmp_path_factory) -> Path:
    """One dir of symlinks to the allowlisted tools, resolved from the REAL PATH once per
    session (workers each build their own; it is a handful of symlinks)."""
    shim = tmp_path_factory.mktemp("shim-bin")
    for tool in SPAWNABLE:
        real = _spawnable(tool)
        if real:
            (shim / tool).symlink_to(real)
    for name in ("python", "python3"):
        (shim / name).symlink_to(sys.executable)
    return shim


@pytest.fixture(autouse=True)
def hermetic_path(request, monkeypatch, _shim_bin):
    if _is_e2e(request):
        return
    monkeypatch.setenv("PATH", str(_shim_bin))


class HostToolSpawned(BaseException):
    """A test was about to execute a real host tool. A BaseException on purpose: the code under
    test catches OSError, and some of it catches Exception (``run_stream`` turns any launch
    failure into rc 127) — a swallowed leak is the failure mode this exists to end, and one that
    reads as a handled error passes the test for the wrong reason. pytest reports it as a
    failure like any other."""


def _resolved(cmd, env, executable=None) -> list[str]:
    """Where this call would execute from: the first argv element resolved the way exec does —
    an absolute/relative path as is, a bare name through the CALLER's PATH (``env`` wins over
    the ambient one, as it does for the child) — plus ``executable`` when given, since that is
    what actually runs (argv[0] is then only the name the child sees). Not found ⇒ omitted."""
    names = []
    if isinstance(cmd, (str, bytes, os.PathLike)):
        names.append(os.fsdecode(cmd).split()[0])
    elif cmd:
        names.append(os.fsdecode(cmd[0]))
    if executable is not None:
        names.append(os.fsdecode(executable))
    path = (env or os.environ).get("PATH", os.environ.get("PATH", ""))
    return [found for exe in names if (found := shutil.which(exe, path=path))]


def _shell_executes(args: list[str]) -> bool:
    """Whether a shell handed ``args`` (its argv[1:]) would RUN anything. ``-n`` / ``-o noexec``
    among the leading options means it only parses; ``--version`` / ``--help`` only print. Any
    other form — ``-c program``, a script path, bare stdin — is a program the guard can't see
    into, so it counts as executing."""
    it = iter(args)
    for arg in it:
        if arg in ("--", "-") or not arg.startswith("-"):
            break
        if arg in ("--version", "--help"):
            return False
        if arg == "-o":
            if next(it, None) == "noexec":
                return False
        elif not arg.startswith("--") and "n" in arg[1:]:
            return False
    return True


@pytest.fixture(autouse=True)
def no_host_tool_spawn(request, monkeypatch):
    if _is_e2e(request):
        return
    # By NAME wherever it lives (git_shim runs the real git by absolute path; the box PATH tests
    # hand bash a `PATH=/usr/bin:/bin` of their own), plus the interpreter and the marker's paths.
    names = set(SPAWNABLE) | {"python", "python3"}
    permitted = {os.path.realpath(sys.executable)}
    shells: set[str] = set()  # a bare name in the marker: programs may run under that shell
    for mark in request.node.iter_markers("spawns"):
        shells.update(p for p in mark.args if os.sep not in p)
        permitted.update(os.path.realpath(p) for p in mark.args if os.sep in p)

    def guard(real):
        def wrapped(cmd, *args, **kwargs):
            # A shell string is a whole program; resolving its first word would miss every
            # later one (`true; /opt/homebrew/bin/podman …`). Nothing under test spawns a shell
            # this way, so the form is refused rather than parsed.
            if kwargs.get("shell"):
                raise HostToolSpawned(
                    f"{request.node.nodeid} would run a shell=True command — spawn an explicit "
                    f"argv instead (the guard resolves argv[0]), or it is an e2e test "
                    f"(tests/*_e2e.py)"
                )
            # `executable` is Popen's third positional (after bufsize) or a kwarg.
            executable = args[1] if len(args) > 1 else kwargs.get("executable")
            resolved = _resolved(cmd, kwargs.get("env"), executable)
            for found in resolved:
                if not (os.path.basename(found) in names or os.path.realpath(found) in permitted):
                    raise HostToolSpawned(
                        f"{request.node.nodeid} would execute {found!r} — stub the call at "
                        f"its own layer (e.g. `<module>.subprocess.run`), mark the test "
                        f"`@pytest.mark.spawns({found!r})` if it means to, or it is an e2e "
                        f"test (tests/*_e2e.py)"
                    )
            # What actually runs is `executable` when given (appended last), else argv[0]. A
            # shell is allowlisted by name for `-n`; a program under it is the shell=True gap.
            runs = resolved[-1] if resolved else ""
            shell = os.path.basename(runs)
            tail = (
                []
                if isinstance(cmd, (str, bytes, os.PathLike))
                else [os.fsdecode(a) for a in cmd[1:]]
            )
            if (
                shell in SHELLS
                and shell not in shells
                and os.path.realpath(runs) not in permitted
                and _shell_executes(tail)
            ):
                raise HostToolSpawned(
                    f"{request.node.nodeid} would run a program under {runs!r} (`{shell} -c …` / "
                    f"`{shell} script` carries a whole program past argv[0]) — spawn the tool "
                    f"directly, mark the test `@pytest.mark.spawns({shell!r})` if it means to, "
                    f"or it is an e2e test (tests/*_e2e.py)"
                )
            return real(cmd, *args, **kwargs)

        return wrapped

    for name in ("run", "Popen", "check_output", "check_call", "call"):
        monkeypatch.setattr(subprocess, name, guard(getattr(subprocess, name)))


@pytest.fixture(autouse=True)
def deterministic_engine(monkeypatch, request):
    """Pin the container engine so golden command tests assert one engine regardless of
    whether the host (CI / box / Mac) has podman or docker on PATH. Engine-detection
    tests override this by deleting FOLDYARD_ENGINE themselves (via fresh_config).

    NOT for the live modules that drive the real CLI: they run the engine actually installed
    (test_e2e's runner names it; anything else the CLI spawns must agree)."""
    if _runs_the_real_cli(request.module):
        return
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
