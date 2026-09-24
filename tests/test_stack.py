"""stack.py — `foldyard shellenv` (the _common.sh replacement) + ensure_dirs/banner. Golden-ish
output checks on the emitted shell, with main_repo / machine / devmode mocked so nothing
touches git, podman, or the real mode state."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from foldyard import config, devmode, machine, stack, supervisor

# Project config the stack reads from foldyard.toml — written into each fake repo so the
# tests are hermetic (don't depend on the real repo's foldyard.toml or where they run).
_FAKE_TOML = """\
[project]
name = "tangible"
prefix = "tangible-podman"
app = "platform-frontend"
compose = ["compose.podman.yml"]
ensure_dirs = ["platform/.db-exports", "platform/.docker/gcs-emulator"]

[ports]
APP_PORT = 3000
SIM_PORT = 4500

# Declares the gcp-metadata namespace (+ project) so the gcp plugin LOADS for this consumer —
# the stack tests exercise its derived mode env (GCP_METADATA_HOST etc.).
[plugins.gcp-metadata]
project = "acme-staging"
"""


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    main = tmp_path / "repo"
    (main / "dev-stack").mkdir(parents=True)
    (main / "foldyard.toml").write_text(_FAKE_TOML)
    (main / "compose.podman.yml").write_text("services: {}\n")  # a real compose project
    # Point config at the fake repo (FOLDYARD_REPO wins) and re-resolve its caches.
    monkeypatch.setenv("FOLDYARD_REPO", str(main))
    config.clear_caches()
    monkeypatch.setattr(stack, "main_repo", lambda: main)
    # CRITICAL: stack.up() calls supervisor.ensure_background(), which SPAWNS A REAL detached
    # `foldyard host`. Unstubbed, `test_up_*` leaks a background daemon that outlives the test —
    # with the test's tmp FY_PORTS_FILE/FOLDYARD_REPO baked in, so it later reallocates a port band
    # and collides with real projects (observed 2026-07: two leaked daemons squatting :41000/:41100).
    monkeypatch.setattr(supervisor, "ensure_background", lambda *a, **k: None)
    # `stack.up()` runs the host preflight (backend CLI / proxy prereqs) first — not these golden
    # tests' subject, and the test host has no limactl, so neuter it here (test_preflight.py covers
    # the checks; test_up_runs_preflight_before_resolving_the_machine covers the wiring).
    from foldyard import preflight

    monkeypatch.setattr(preflight, "check_or_abort", lambda *a, **k: None)
    monkeypatch.setattr(machine, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(machine, "socket", lambda: "unix:///fake.sock")
    monkeypatch.setattr(devmode, "read", lambda *a, **k: {"mode": {"gcp": "off", "github": "off"}})
    # A DEAD socket path, deliberately: inside the dev box unix:///var/run/docker.sock is the
    # machine's LIVE engine socket, and this fixture's project prefix is the REAL stack's name —
    # so any test call that escapes the subprocess mocks (podman-remote is installed in the box
    # and honours CONTAINER_HOST) acts on the real stack. An escaped `podman rm -f` from the
    # reconcile tests deleted the box's entire running dev stack (2026-08). A nonexistent socket
    # makes every escaped engine call fail instead.
    dead_socket = "unix:///nonexistent/fy-tests.sock"
    monkeypatch.setenv("DOCKER_HOST", dead_socket)
    monkeypatch.setenv("CONTAINER_HOST", dead_socket)
    # _podman_build streams build output into <state_dir>/build-<project>.log — keep it in tmp.
    monkeypatch.setenv("FOLDYARD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("WORKTREE", raising=False)
    monkeypatch.delenv("FOLDYARD_WORKTREES_ROOT", raising=False)
    monkeypatch.delenv("FOLDYARD_COMPOSE_EXTRA", raising=False)
    monkeypatch.delenv("PODMAN_COMPOSE_PROVIDER", raising=False)
    monkeypatch.setattr(stack, "compose_provider_path", lambda: "/fy-venv/bin/podman-compose")
    yield main
    config.clear_caches()


def test_main_repo_shells_out_once_per_process(tmp_path, monkeypatch):
    """main_repo() shells out to git, and the TUI's 1s refresh timer resolves a worktree config
    (→ here) ON THE EVENT LOOP. Uncached, one git call per tick per worktree stalls the whole UI —
    and in the box, where git goes through the index-split shim, a stale index makes each call
    ~3s, so the timer can never finish a tick. That starvation is what made the TUI suite take
    282s and flake 5 modal tests on `NoMatches` (the modal's on_mount racing its own compose).
    Cached, one resolution serves the process; `config.clear_caches()` invalidates it."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, f"{tmp_path}/.git\n", "")

    monkeypatch.setattr(stack, "_run", fake_run)
    config.clear_caches()
    assert stack.main_repo() == tmp_path
    for _ in range(20):  # a TUI tick storm
        stack.main_repo()
    assert len(calls) == 1, f"main_repo shelled out {len(calls)}× — the cache is gone"
    config.clear_caches()  # …but a relocated repo must still re-resolve
    stack.main_repo()
    assert len(calls) == 2


def _worktree(fake_repo: Path, name: str) -> Path:
    """A sibling worktree checkout carrying the fixture's declared compose file — the stack is
    the worktree's, so that is where the file must be."""
    wt = Path(f"{fake_repo}-worktrees") / name
    wt.mkdir(parents=True)
    (wt / "compose.podman.yml").write_text("services: {}\n")
    return wt


def test_shellenv_main_path(fake_repo, capsys):
    rc = stack.shellenv()
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines()[0] == "set -euo pipefail"
    assert any(line.startswith("cd ") for line in out.splitlines())
    assert "export FOLDYARD_CHECKOUT=" in out and str(fake_repo) in out
    # MAIN_REPO is exported (not a plain var) so shell-driven `compose up` subprocesses see it —
    # consumer compose files reference it for resources shared across worktrees (e.g. DB dumps).
    assert f"export MAIN_REPO={fake_repo}\n" in out
    assert "export PODMAN_PROJECT=" in out and "tangible-podman" in out
    assert "export COMPOSE_PROJECT_NAME=" in out
    assert "export DOCKER_HOST=" in out and "unix:///nonexistent/fy-tests.sock" in out
    # podman reads CONTAINER_HOST — same socket, so podman drives the engine everywhere.
    assert "export CONTAINER_HOST=" in out and out.count("unix:///nonexistent/fy-tests.sock") >= 2
    assert "ENGINE=podman" in out  # consumer recipes echo `"$ENGINE" …`
    assert "export PODMAN_COMPOSE_PROVIDER=/fy-venv/bin/podman-compose" in out
    assert "COMPOSE=(podman compose -f " in out and "compose.podman.yml" in out
    assert "dev_vm_banner() {" in out
    assert "ensure_stubs" not in out  # the shim went with the hardcoded GCP stubs it created
    # Main path exports the [ports] bases at offset 0 (APP_PORT=3000, SIM_PORT=4500, …) so they're
    # SET in the compose env — matching the compose `${VAR:-default}` fallbacks, and silencing
    # compose-go's "variable is not set" warning (which fires even on defaulted refs).
    assert "export APP_PORT=3000" in out and "export SIM_PORT=4500" in out


def test_shellenv_preserves_explicit_compose_provider(fake_repo, capsys, monkeypatch):
    monkeypatch.setenv("PODMAN_COMPOSE_PROVIDER", "/custom/podman-compose")
    assert stack.shellenv() == 0
    assert "export PODMAN_COMPOSE_PROVIDER=/custom/podman-compose" in capsys.readouterr().out


def test_shellenv_worktree_path(fake_repo, capsys, monkeypatch):
    wt = _worktree(fake_repo, "feat")
    monkeypatch.setenv("WORKTREE", "feat")
    monkeypatch.setattr(stack, "_offset", lambda name: 7)
    rc = stack.shellenv()
    out = capsys.readouterr().out
    assert rc == 0
    assert "tangible-podman-feat" in out  # namespaced project
    assert "APP_PORT=3007" in out  # 3000 + offset (shlex.quote leaves digits unquoted)
    assert "SIM_PORT=4507" in out
    assert str(wt) in out  # FOLDYARD_CHECKOUT points at the worktree tree
    # COMPOSE names the WORKTREE's file, absolute: the recipe `cd`s to MAIN_REPO, so a relative
    # `-f` there pointed the worktree's stack at main's compose file.
    assert f"-f {wt / 'compose.podman.yml'}" in out


def test_shellenv_without_a_declared_stack_still_emits_the_env(stackless_repo, capsys):
    # A box-only project has nothing to validate: the shim still emits the shared env (the raw
    # recipes need MAIN_REPO/DOCKER_HOST/ports), with a COMPOSE that names no `-f` file.
    assert stack.shellenv() == 0
    out = capsys.readouterr().out
    assert "export FOLDYARD_CHECKOUT=" in out and "COMPOSE=(podman compose)\n" in out


def test_shellenv_worktree_missing_compose_file_aborts(fake_repo, capture_run, capsys, monkeypatch):
    # Declared but not in the worktree (main's copy is): the eval'd `exit 1` + foldyard's own
    # error naming the file and the checkout, before any raw recipe hands the provider the path.
    # And BEFORE _context(): on a host with no preset DOCKER_HOST that would ensure — provision —
    # the machine for a stack that can't run, so neither the machine nor the engine (nor
    # _context's git call) may be touched.
    wt = Path(f"{fake_repo}-worktrees") / "feat"
    wt.mkdir(parents=True)
    monkeypatch.setenv("WORKTREE", "feat")
    monkeypatch.setattr(stack, "_offset", lambda name: 7)
    monkeypatch.delenv("DOCKER_HOST")  # the host path: _docker_host would ensure the machine
    monkeypatch.setattr(stack, "which", lambda name: "/fake/bin/podman")
    ensured: list[tuple] = []
    monkeypatch.setattr(machine, "ensure", lambda *a, **k: ensured.append(a))
    assert stack.shellenv() == 1
    out, err = capsys.readouterr()
    assert "exit 1" in out and "COMPOSE=(" not in out
    assert "compose.podman.yml" in err and str(wt) in err
    assert ensured == [] and capture_run == []


def test_offset_precedence_env_then_pin_then_cksum(fake_repo, monkeypatch):
    # A worktree can be pinned to a fixed SMALL offset via the MAIN checkout's gitignored
    # foldyard.local.toml [worktree-offsets] — e.g. to land its app on a host port some external
    # service's allowlist accepts. Precedence: WT_OFFSET env > pin > cksum.
    monkeypatch.delenv("WT_OFFSET", raising=False)
    assert 1 <= stack._offset("feat") <= 89  # no pin → deterministic cksum default
    (fake_repo / "foldyard.local.toml").write_text("[worktree-offsets]\nfeat = 2\n")
    assert stack._offset("feat") == 2  # the pin wins over cksum
    assert 1 <= stack._offset("other-wt") <= 89  # an unpinned worktree still uses cksum
    monkeypatch.setenv("WT_OFFSET", "7")
    assert stack._offset("feat") == 7  # explicit env still wins over the pin


def test_shellenv_worktree_inferred_from_cwd(fake_repo, capsys, monkeypatch):
    # No WORKTREE env, but cwd is inside a sibling worktree → infer its name (so `cd`-ing into a
    # worktree means its verbs Just Work without the WORKTREE= prefix).
    wt = _worktree(fake_repo, "feat")
    (wt / "platform").mkdir()
    monkeypatch.setattr(stack, "_offset", lambda name: 7)
    monkeypatch.chdir(wt / "platform")  # a nested dir under the worktree, not just its root
    rc = stack.shellenv()
    out = capsys.readouterr().out
    assert rc == 0
    assert "tangible-podman-feat" in out  # namespaced project, inferred from cwd
    assert "APP_PORT=3007" in out
    assert str(wt) in out


def test_shellenv_explicit_env_overrides_cwd(fake_repo, capsys, monkeypatch):
    # An explicit WORKTREE wins even when cwd sits in a different worktree.
    _worktree(fake_repo, "other")
    elsewhere = _worktree(fake_repo, "feat")
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("WORKTREE", "other")
    monkeypatch.setattr(stack, "_offset", lambda name: 7)
    rc = stack.shellenv()
    out = capsys.readouterr().out
    assert rc == 0
    assert "tangible-podman-other" in out  # env wins over the cwd's 'feat'


def test_shellenv_main_cwd_outside_worktrees_root(fake_repo, capsys, monkeypatch):
    # Standing in the main checkout (a sibling of, not under, the worktrees root) → main path,
    # exactly as if no worktree were involved: no port offsets, checkout is the main repo.
    monkeypatch.chdir(fake_repo)
    rc = stack.shellenv()
    out = capsys.readouterr().out
    assert rc == 0
    assert "export PODMAN_PROJECT=tangible-podman\n" in out  # no -<worktree> suffix
    assert f"export FOLDYARD_CHECKOUT={fake_repo}\n" in out
    assert "export APP_PORT=3000" in out  # main path → [ports] bases at offset 0 (no per-wt offset)


def test_shellenv_worktree_missing_aborts(fake_repo, capsys, monkeypatch):
    monkeypatch.setenv("WORKTREE", "ghost")
    rc = stack.shellenv()
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out.strip() == "exit 1"  # the recipe's eval runs this → aborts
    assert "no worktree" in captured.err


def test_shellenv_no_machine_omits_docker_host(fake_repo, capsys, monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    rc = stack.shellenv(no_machine=True)
    out = capsys.readouterr().out
    assert rc == 0 and "DOCKER_HOST" not in out and "CONTAINER_HOST" not in out


def test_shellenv_emits_mode_env(fake_repo, capsys, monkeypatch):
    monkeypatch.setattr(devmode, "read", lambda *a, **k: {"mode": {"gcp": "logs", "github": "off"}})
    stack.shellenv()
    out = capsys.readouterr().out
    # emitted as ${K:-v} so an explicit env var still wins
    assert 'export GCP_METADATA_HOST="${GCP_METADATA_HOST:-metadata-emulator:80}"' in out
    assert 'export COMPOSE_PROFILES="${COMPOSE_PROFILES:-metadata}"' in out


# ── the shared .git/config: written once per checkout, never per invocation (issue #6) ──
#
# `.git/config` is on the virtiofs mount and rewritten by git's lock→rename protocol, which is
# not atomic across kernels (ADR-0021's index race). foldyard was the file's most frequent
# writer — `core.fileMode=false` on EVERY shellenv/resolve, from BOTH kernels — and a lost
# update once left a consumer's config as the 25-byte `[core] fileMode = false`. The pin must
# stay in the FILE (a host GUI's bundled git — Fork's own 2.50 — reads neither shellenv nor the
# box shim), so the fix is read-first: git rewrites the file even for an unchanged value.


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def real_repo(tmp_path):
    repo = tmp_path / "real"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "git@example.com:acme/app.git")
    return repo


def test_pin_filemode_writes_once_and_keeps_the_rest_of_the_config(real_repo):
    stack.pin_filemode(real_repo)
    assert _git(real_repo, "config", "--get", "core.fileMode") == "false"
    assert _git(real_repo, "remote", "get-url", "origin") == "git@example.com:acme/app.git"


def test_pin_filemode_does_not_rewrite_an_already_pinned_config(real_repo):
    _git(real_repo, "config", "core.fileMode", "false")
    cfg = real_repo / ".git" / "config"
    before = cfg.stat()
    stack.pin_filemode(real_repo)
    after = cfg.stat()
    # A `git config` write replaces the file (lock → rename), so the inode changes — the
    # original still exists while the replacement is created. Same inode ⇒ no write happened.
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_pin_filemode_still_writes_when_pinned_to_true(real_repo):
    _git(real_repo, "config", "core.fileMode", "true")
    stack.pin_filemode(real_repo)
    assert _git(real_repo, "config", "--get", "core.fileMode") == "false"


def test_pin_filemode_reads_the_repo_local_value_only(real_repo, monkeypatch, tmp_path):
    # A global `core.fileMode=false` (a host operator's own dotfiles) must not skip the write:
    # the box's git has no such global, so the SHARED file is the only place both sides agree.
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[core]\n\tfileMode = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    stack.pin_filemode(real_repo)
    assert _git(real_repo, "config", "--local", "--get", "core.fileMode") == "false"


def test_context_pins_filemode_through_the_helper(fake_repo, monkeypatch):
    seen: list[Path] = []
    monkeypatch.setattr(stack, "pin_filemode", lambda main: seen.append(main))
    stack._context(no_machine=True)
    assert seen == [stack.main_repo()]


def test_ensure_dirs_creates_declared_dirs_only(tmp_path, monkeypatch):
    """`fy up` pre-creates what the consumer DECLARES ([project].ensure_dirs) and nothing
    else — no hardcoded `.stubs/` GCP credential files."""
    (tmp_path / "foldyard.toml").write_text(
        '[project]\nname = "p"\nensure_dirs = ["platform/.db-exports", "platform/.docker/gcs-emulator"]\n'
    )
    monkeypatch.setenv("FOLDYARD_REPO", str(tmp_path))
    config.clear_caches()
    stack._ensure_dirs(tmp_path)
    assert (tmp_path / "platform/.db-exports").is_dir()
    assert (tmp_path / "platform/.docker/gcs-emulator").is_dir()
    assert not (tmp_path / ".stubs").exists()  # nothing undeclared
    config.clear_caches()


def test_ensure_dirs_writes_nothing_undeclared(tmp_path, monkeypatch):
    """A consumer with no ensure_dirs gets no files at all — the old hardcoded
    `<dev_vm_dir>/.stubs/` (a Tangible-specific leftover) is gone."""
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "p"\n')
    monkeypatch.setenv("FOLDYARD_REPO", str(tmp_path))
    config.clear_caches()
    stack._ensure_dirs(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["foldyard.toml"]
    config.clear_caches()


def test_banner_reads_env(capsys, monkeypatch):
    monkeypatch.setenv("PODMAN_PROJECT", "proj")
    monkeypatch.setenv("FOLDYARD_CHECKOUT", "/r")
    monkeypatch.setenv("DOCKER_HOST", "unix:///s")
    monkeypatch.setenv("FOLDYARD_ENV_OVERRIDE", "/e")
    stack.banner()
    out = capsys.readouterr().out
    assert "PODMAN_PROJECT=proj" in out and "/r" in out and "unix:///s" in out and "/e" in out


# ── engine verbs: golden command sequences (engine mocked) ─────────────────────────────


class _FakeProc:
    returncode = 0
    stdout = ""
    stderr = ""


@pytest.fixture
def capture_run(monkeypatch):
    """Record every subprocess.run command and return success, so verbs run without a real
    engine. Returns the (mutated-in-place) list of all recorded commands."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _FakeProc()

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    return calls


def _composes(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if "compose" in c]


def test_ps_runs_compose_ps(fake_repo, capture_run):
    assert stack.ps() == 0
    last = _composes(capture_run)[-1]
    assert last[-1] == "ps"
    assert "-f" in last and any("compose.podman.yml" in x for x in last)


def test_teardown_verbs_do_not_provision_a_missing_machine(
    fake_repo, capture_run, monkeypatch, capsys
):
    # `fy down` after the machine was deleted must NOT start downloading a VM image: the
    # read/teardown verbs short-circuit instead of letting resolve() ensure the machine.
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(
        machine, "not_running_reason", lambda: "podman machine 'tangible' does not exist"
    )
    ensured: list = []
    monkeypatch.setattr(machine, "ensure", lambda *a, **k: ensured.append(a))
    assert stack.down() == 0
    assert stack.ps() == 0
    assert stack.logs([]) == 0
    assert stack.nuke() == 0
    assert capture_run == [] and ensured == []  # no compose, no machine create/start
    err = capsys.readouterr().err
    assert "nothing to stop" in err and "nothing to nuke" in err


def test_verbs_that_need_a_running_stack_do_not_boot_a_stopped_machine(
    fake_repo, capture_run, monkeypatch, capsys
):
    # `fy shell` execs into a running app and the supervisor's heal restarts running services —
    # a stopped VM holds neither, so neither may ensure it (the heal runs HEADLESS on the host).
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(machine, "not_running_reason", lambda: "lima machine 'x' is stopped")
    ensured: list = []
    monkeypatch.setattr(machine, "ensure", lambda *a, **k: ensured.append(a))
    assert stack.shell() == 1
    assert stack.recreate_services(["api"]) == (
        False,
        "lima machine 'x' is stopped — nothing to recreate",
    )
    assert capture_run == [] and ensured == []
    assert "nothing to exec into" in capsys.readouterr().err


def test_engine_reachable_trusts_an_inherited_docker_host(fake_repo, monkeypatch):
    # In the box / CI, DOCKER_HOST is preset — the gate must not consult the machine at all.
    monkeypatch.setattr(machine, "not_running_reason", lambda: "should not be called")
    assert stack.engine_reachable("stop") is True


def test_engine_reachable_names_the_verb_that_starts_it(fake_repo, monkeypatch, capsys):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(machine, "not_running_reason", lambda: "lima machine 'x' is stopped")
    assert stack.engine_reachable("attach to", "fy box up") is False
    assert "nothing to attach to. `fy box up` creates/starts it." in capsys.readouterr().err


def test_down_runs_compose_down(fake_repo, capture_run):
    assert stack.down() == 0
    # --remove-orphans reaps profile-gated leftovers (e.g. the metadata emulator) on posture switch.
    assert _composes(capture_run)[-1][-2:] == ["down", "--remove-orphans"]


@pytest.fixture
def foreign_stack(monkeypatch):
    """An engine where the project runs TWO containers: `bbb` visible to the active provider
    (it matches the signature-label filter) and `aaa` created by a DIFFERENT compose provider
    (project label only) — the state a provider switch leaves behind. Records every command."""
    import types

    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        # The sweep's probes are direct-engine `<engine> ps -a --filter …` (compose verbs have
        # "compose" at cmd[1] and never match). The signature-filtered probe sees only `bbb`.
        if len(cmd) >= 2 and cmd[1] == "ps" and "-a" in cmd:
            signature = any(
                f.startswith("label=io.podman.compose.project=")
                or f == "label=com.docker.compose.config-hash"
                for f in cmd
            )
            out = "bbb app\n" if signature else "aaa postgres\nbbb app\n"
            return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
        return _FakeProc()

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    return calls


def test_up_sweeps_foreign_provider_containers_before_starting(fake_repo, foreign_stack):
    # A stack created by the PREVIOUS compose provider (docker-compose) is invisible to the
    # bundled podman-compose — without the sweep, `up` dies on "name already in use" + pod
    # membership errors instead of reconciling. The foreign container is removed; the native
    # one is left for compose itself.
    assert stack.up() == 0
    rm = next(c for c in foreign_stack if c[:3] == ["podman", "rm", "-f"])
    assert rm[3:] == ["aaa"]
    up_idx = next(i for i, c in enumerate(foreign_stack) if "compose" in c and "up" in c)
    assert foreign_stack.index(rm) < up_idx
    # The native probe keyed on podman-compose's OWN project label (docker-compose never writes it).
    assert any(
        len(c) >= 2 and c[1] == "ps" and "label=io.podman.compose.project=tangible-podman" in c
        for c in foreign_stack
    )


def test_up_leaves_an_all_native_stack_alone(fake_repo, capture_run):
    # capture_run answers the sweep probes with EMPTY output — nothing foreign, nothing removed.
    assert stack.up() == 0
    assert not any(c[:2] == ["podman", "rm"] for c in capture_run)


def test_up_runs_preflight_before_resolving_the_machine(fake_repo, capture_run, monkeypatch):
    # `fy up` must consult the preflight BEFORE resolve() ensures the machine — that gate is what
    # stops an unmet prerequisite (e.g. the default lima backend with no limactl) from reaching
    # engine resolution, where an unset DOCKER_HOST/CONTAINER_HOST silently falls back to the
    # host's own podman socket. Mirrors test_box.test_up_runs_preflight_before_assembling.
    from foldyard import preflight

    def boom(context):
        assert context == "fy up"
        raise SystemExit(1)

    monkeypatch.setattr(preflight, "check_or_abort", boom)
    with pytest.raises(SystemExit):
        stack.up()
    assert capture_run == []  # aborted before any engine command


def test_down_sweeps_foreign_provider_containers(fake_repo, foreign_stack):
    # `down` is as blind as `up`: without the sweep a foreign-provider stack survives it wholesale.
    assert stack.down() == 0
    rm = next(c for c in foreign_stack if c[:3] == ["podman", "rm", "-f"])
    assert rm[3:] == ["aaa"]
    assert any("compose" in c and "down" in c for c in foreign_stack)


def test_sweep_signature_on_docker_engine_is_the_config_hash(fake_repo, foreign_stack, monkeypatch):
    # docker-compose is the only provider writing com.docker.compose.config-hash — on the docker
    # engine (box/CI) that existence-filter is the native signature, so podman-compose-created
    # containers are the foreign ones there.
    monkeypatch.setenv("FOLDYARD_ENGINE", "docker")
    config.clear_caches()
    assert stack.up() == 0
    assert any(
        len(c) >= 2
        and c[0] == "docker"
        and c[1] == "ps"
        and "label=com.docker.compose.config-hash" in c
        for c in foreign_stack
    )
    rm = next(c for c in foreign_stack if c[:3] == ["docker", "rm", "-f"])
    assert rm[3:] == ["aaa"]


@pytest.fixture
def running_services(monkeypatch):
    """What the engine reports running, for the heal's filter — mutate the set in the test."""
    running = {"queue-worker", "graph-api"}
    monkeypatch.setattr(stack, "_running_services", lambda ctx, deadline=None: running)
    return running


def test_recreate_services_force_recreates_on_the_current_posture(
    fake_repo, capture_run, running_services
):
    # The capability-heal resnapshot action (#33): `compose restart` reboots the EXISTING
    # container with its create-time env, cementing whatever posture it was created under — so
    # the heal recreates the named services from the current -f list instead. Headless: never
    # builds, never drags dependencies up, output captured, never raises.
    ok, summary = stack.recreate_services(["queue-worker", "graph-api"])
    assert ok is True and summary == "recreated queue-worker, graph-api"
    last = _composes(capture_run)[-1]
    assert last[-7:] == [
        "up",
        "-d",
        "--no-build",
        "--force-recreate",
        "--no-deps",
        "queue-worker",
        "graph-api",
    ]
    assert "restart" not in last
    assert "-f" in last and any("compose.podman.yml" in x for x in last)


def test_recreate_services_leaves_services_that_are_not_running_alone(
    fake_repo, capture_run, running_services
):
    # A heal refreshes what runs; it never STARTS anything. An explicitly named service is created
    # by `up` even when it never ran (and under docker compose its inactive profile is enabled for
    # it), and with --no-deps its dependencies would not come up either.
    ok, summary = stack.recreate_services(["queue-worker", "asset-tape-extract"])
    assert ok is True
    assert summary == "recreated queue-worker; not running, left alone: asset-tape-extract"
    last = _composes(capture_run)[-1]
    assert last[-1] == "queue-worker" and "asset-tape-extract" not in last


def test_recreate_services_with_nothing_running_runs_no_compose(
    fake_repo, capture_run, running_services
):
    running_services.clear()
    assert stack.recreate_services(["queue-worker"]) == (
        True,
        "none of queue-worker running — nothing to recreate",
    )
    assert not any("up" in c for c in _composes(capture_run))


def test_recreate_services_refuses_when_it_cannot_see_what_runs(
    fake_repo, capture_run, monkeypatch
):
    # Fail-safe: an unreadable engine must not turn into "recreate everything named".
    monkeypatch.setattr(stack, "_running_services", lambda ctx, deadline=None: None)
    ok, summary = stack.recreate_services(["queue-worker"])
    assert ok is False and "couldn't list running services" in summary
    assert not any("up" in c for c in _composes(capture_run))


def test_recreate_services_bounds_every_compose_call_by_one_deadline(fake_repo, monkeypatch):
    # The heal runs on a supervisor worker whose in-flight key clears only when this returns, so
    # the probes BEFORE the up (what runs, which profiles) must be bounded too, and by the same
    # budget — not each by a fresh one.
    import types

    timeouts: list[float | None] = []

    def fake_run(cmd, **kw):
        timeouts.append(kw.get("timeout"))
        out = ""
        if "ps" in cmd:
            out = '{"Service":"queue-worker"}\n'
        if cmd[-1] == "--profiles":
            out = "data\n"
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(stack, "_run", fake_run)
    ok, _ = stack.recreate_services(["queue-worker"], timeout=30.0)
    assert ok is True
    assert len(timeouts) >= 4  # ps, the profile probes, the up
    assert all(t is not None and 0 < t <= 30.0 for t in timeouts), timeouts


def test_recreate_services_spans_running_extra_profiles(
    fake_repo, capture_run, monkeypatch, running_services
):
    # Resnapshot services are typically profile-gated workers (`data`); a provider that drops a
    # service outside the requested profiles would otherwise make the heal a silent no-op.
    monkeypatch.setattr(stack, "_running_extra_profiles", lambda ctx, deadline=None: ["data"])
    ok, _ = stack.recreate_services(["queue-worker"])
    last = _composes(capture_run)[-1]
    assert ok is True
    assert last[last.index("--profile") + 1] == "data" and last.index("--profile") < last.index(
        "up"
    )


def test_recreate_services_reports_failure_instead_of_raising(fake_repo, monkeypatch):
    def _boom(cmd, **kw):
        raise OSError("engine gone")

    monkeypatch.setattr(stack.subprocess, "run", _boom)
    ok, summary = stack.recreate_services(["queue-worker"])
    assert ok is False and "engine gone" in summary


def test_logs_follows_with_service(fake_repo, capture_run):
    stack.logs(["postgres"])
    assert _composes(capture_run)[-1][-5:] == ["logs", "-f", "-n", "50", "postgres"]


def test_shell_execs_app_bash(fake_repo, capture_run):
    stack.shell()
    assert _composes(capture_run)[-1][-3:] == ["exec", "platform-frontend", "bash"]


def test_up_builds_then_ps(fake_repo, capture_run):
    assert stack.up() == 0
    composes = _composes(capture_run)
    # podman engine (pinned): resolve the build graph (`compose config`) + build
    # natively, THEN `up --no-build` (never the classic-builder provider). The fixture's empty
    # compose has no build services, so the up just starts pulled images.
    assert any(c[-1] == "config" for c in composes)  # build-graph resolution
    up_idx = next(i for i, c in enumerate(composes) if "up" in c)
    assert composes[up_idx][-3:] == ["up", "-d", "--no-build"]
    assert "--profile" not in composes[up_idx]  # no running extras → env-driven profiles alone
    assert composes[-1][-1] == "ps"


def _run_returning(monkeypatch, config_yaml: str):
    """Record subprocess.run commands; answer the normalized `compose config` probe with
    ``config_yaml`` (JSON is valid YAML) and everything else with success."""
    import types

    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "config" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=config_yaml, stderr="")
        return _FakeProc()

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    return calls


def test_up_podman_builds_additional_contexts_natively(fake_repo, monkeypatch):
    # The core fix: on the podman engine a service with compose `additional_contexts` is built with
    # native `podman build --build-context` (buildah) — NOT `compose --build`, which podman forces
    # onto the classic builder that rejects additional contexts. Tag = `<project>_<service>` so the
    # subsequent `up --no-build` reuses it.
    cfg = json.dumps(
        {
            "services": {
                "queue-worker": {
                    "build": {
                        "context": "/repo/data",
                        "dockerfile": "Dockerfile",
                        "target": "base",
                        "args": {"FOO": "bar"},
                        "additional_contexts": {"shared": "/repo/data"},
                    }
                },
                "postgres": {"image": "docker.io/pgvector"},  # image-only → never built
            }
        }
    )
    calls = _run_returning(monkeypatch, cfg)
    assert stack.up() == 0
    builds = [c for c in calls if len(c) >= 2 and c[1] == "build"]
    assert len(builds) == 1  # only the build service, not the image-only postgres
    b = builds[0]
    assert b[0] == "podman"  # native buildah, not `compose build`
    assert b[:4] == ["podman", "build", "-t", "tangible-podman_queue-worker"]
    assert "--build-context" in b and "shared=/repo/data" in b
    assert b[b.index("--target") + 1] == "base"
    assert "FOO=bar" in b
    assert b[-1] == "/repo/data"  # context is the final positional arg
    assert b[b.index("-f") + 1] == "/repo/data/Dockerfile"  # dockerfile resolved onto context
    # up must NOT rebuild via the provider.
    up = next(c for c in calls if "compose" in c and "up" in c)
    assert up[-1] == "--no-build"


def test_podman_builds_services_concurrently(fake_repo, monkeypatch):
    import threading
    import types

    cfg = json.dumps(
        {
            "services": {
                "svc-a": {"build": {"context": "/repo/a"}},
                "svc-b": {"build": {"context": "/repo/b"}},
            }
        }
    )
    rendezvous = threading.Barrier(2)
    built: list[str] = []

    def fake_run(cmd, **kw):
        if "config" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=cfg, stderr="")
        if len(cmd) >= 2 and cmd[1] == "build":
            built.append(cmd[-1])
            rendezvous.wait(timeout=2)  # a sequential implementation times out here
        return _FakeProc()

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    assert stack.build([]) == 0
    assert set(built) == {"/repo/a", "/repo/b"}


def test_podman_build_deduplicates_identical_specs_and_tags_each_service(fake_repo, monkeypatch):
    cfg = json.dumps(
        {
            "services": {
                "svc-a": {"build": {"context": "/repo/shared", "target": "base"}},
                "svc-b": {"build": {"context": "/repo/shared", "target": "base"}},
            }
        }
    )
    calls = _run_returning(monkeypatch, cfg)

    assert stack.build([]) == 0
    builds = [c for c in calls if len(c) >= 2 and c[1] == "build"]
    tags = [c for c in calls if len(c) >= 2 and c[1] == "tag"]
    assert len(builds) == 1
    assert builds[0][3] == "tangible-podman_svc-a"
    assert tags == [["podman", "tag", "tangible-podman_svc-a", "tangible-podman_svc-b"]]


def test_podman_build_splits_identical_specs_with_distinct_platforms(fake_repo, monkeypatch):
    # Two services share one normalized build mapping but declare different service-level
    # `platform` targets (no build.platforms): they must NOT share a build — each gets its own
    # `podman build --platform <target>`, and no alias tag crosses architectures.
    cfg = json.dumps(
        {
            "services": {
                "svc-amd": {
                    "platform": "linux/amd64",
                    "build": {"context": "/repo/shared", "target": "base"},
                },
                "svc-arm": {
                    "platform": "linux/arm64",
                    "build": {"context": "/repo/shared", "target": "base"},
                },
            }
        }
    )
    calls = _run_returning(monkeypatch, cfg)

    assert stack.build([]) == 0
    builds = [c for c in calls if len(c) >= 2 and c[1] == "build"]
    assert len(builds) == 2
    assert not [c for c in calls if len(c) >= 2 and c[1] == "tag"]
    platforms = {c[3]: c[c.index("--platform") + 1] for c in builds}
    assert platforms == {
        "tangible-podman_svc-amd": "linux/amd64",
        "tangible-podman_svc-arm": "linux/arm64",
    }


def test_podman_build_redacts_build_arg_values_in_echo_and_log(fake_repo, monkeypatch, capsys):
    # Resolved build args can carry secrets. The argv handed to the engine keeps the real value;
    # the echoed `+ podman build …` line and the persistent build log both show `K=<redacted>`.
    cfg = json.dumps(
        {
            "services": {
                "app": {
                    "build": {
                        "context": "/repo/app",
                        "args": {"NPM_TOKEN": "sekret-sentinel", "PLAIN": None},
                    }
                }
            }
        }
    )
    calls = _run_returning(monkeypatch, cfg)

    assert stack.build([]) == 0
    build = next(c for c in calls if len(c) >= 2 and c[1] == "build")
    assert "NPM_TOKEN=sekret-sentinel" in build  # the real build still gets the value
    err = capsys.readouterr().err
    log_text = (config.state_dir() / "build-tangible-podman.log").read_text()
    for surface in (err, log_text):
        assert "sekret-sentinel" not in surface
        assert "NPM_TOKEN=<redacted>" in surface
        assert "PLAIN" in surface  # a valueless (env-passthrough) arg has nothing to redact


def test_walled_stack_build_is_a_trusted_build(fake_repo, monkeypatch):
    # Same concession as `fy box build` (ADR-0029's amendment): a stack image build has no proxy
    # CA either, so it reaches the proxy with the build marker — tunnelled, still walled. Before
    # the service's own args, so a service that sets a proxy arg wins.
    from foldyard.plugins import proxy

    monkeypatch.setattr(config, "machine_wall", lambda: True)
    cfg = json.dumps(
        {"services": {"app": {"build": {"context": "/repo/app", "args": {"HTTPS_PROXY": "mine"}}}}}
    )
    calls = _run_returning(monkeypatch, cfg)
    assert stack.build([]) == 0
    build = next(c for c in calls if len(c) >= 2 and c[1] == "build")
    args = [build[i + 1] for i, tok in enumerate(build) if tok == "--build-arg"]
    marker = f"http://{proxy.BUILD_TUNNEL_USER}:"
    assert any(a.startswith(f"https_proxy={marker}") for a in args)
    assert any(a.startswith(f"HTTPS_PROXY={marker}") for a in args)
    assert f"https_proxy={proxy.build_proxy_url()}" not in args  # a secret, not the bare marker
    assert args[-1] == "HTTPS_PROXY=mine"


def test_unwalled_stack_build_gets_no_proxy_args(fake_repo, monkeypatch):
    monkeypatch.setattr(config, "machine_wall", lambda: False)
    calls = _run_returning(
        monkeypatch, json.dumps({"services": {"app": {"build": {"context": "/a"}}}})
    )
    assert stack.build([]) == 0
    build = next(c for c in calls if len(c) >= 2 and c[1] == "build")
    assert "--build-arg" not in build


def test_stack_builds_run_under_the_build_gate(fake_repo, monkeypatch):
    # Both entry points — `fy build` and `fy up` — hand their build to the gate, which reports
    # and offers what the wall refused, then retries.
    from foldyard import buildgate

    gated: list[str] = []

    def fake_gate(build, *, what, **_):
        gated.append(what)
        return build(None)

    monkeypatch.setattr(buildgate, "run", fake_gate)
    _run_returning(monkeypatch, json.dumps({"services": {"app": {"build": {"context": "/a"}}}}))
    assert stack.build([]) == 0
    assert gated == ["stack build"]


def test_up_docker_engine_builds_then_starts_without_rebuilding(
    fake_repo, capture_run, monkeypatch
):
    # Docker (e.g. CI) delegates the build to Compose/BuildKit, then starts the image with
    # --no-build so up follows the same shared engine-selection path as explicit builds.
    monkeypatch.setenv("FOLDYARD_ENGINE", "docker")
    config.clear_caches()
    assert stack.up() == 0
    calls = capture_run
    assert not any(len(c) >= 2 and c[1] == "build" for c in calls)  # no native podman build
    assert not any("config" in c and "--format" in c for c in calls)  # no build-graph resolution
    compose_build = next(c for c in _composes(calls) if "build" in c)
    assert compose_build[-1] == "build"
    up = next(c for c in _composes(calls) if "up" in c)
    assert up[-3:] == ["up", "-d", "--no-build"]


def test_build_podman_uses_native_builder_for_requested_profile_service(fake_repo, monkeypatch):
    cfg = json.dumps(
        {
            "services": {
                "e2e-app": {
                    "build": {
                        "context": "/repo/platform",
                        "dockerfile": "Dockerfile",
                        "target": "base",
                    }
                },
                "platform-frontend": {
                    "build": {
                        "context": "/repo/platform",
                        "dockerfile": "Dockerfile",
                        "target": "base",
                    }
                },
            }
        }
    )
    calls = _run_returning(monkeypatch, cfg)

    assert stack.build(["e2e-app"], extra_profiles=["e2e"]) == 0

    builds = [c for c in calls if len(c) >= 2 and c[1] == "build"]
    assert builds == [
        [
            "podman",
            "build",
            "-t",
            "tangible-podman_e2e-app",
            "-f",
            "/repo/platform/Dockerfile",
            "--target",
            "base",
            "/repo/platform",
        ]
    ]
    config_call = next(c for c in _composes(calls) if "config" in c)
    assert config_call[-3:] == ["--profile", "e2e", "config"]


def test_build_docker_delegates_to_compose(fake_repo, capture_run, monkeypatch):
    monkeypatch.setenv("FOLDYARD_ENGINE", "docker")
    config.clear_caches()

    assert stack.build(["e2e-app"], extra_profiles=["e2e"]) == 0

    assert _composes(capture_run)[-1][-4:] == ["--profile", "e2e", "build", "e2e-app"]


def test_build_rejects_unknown_podman_service(fake_repo, monkeypatch, capsys):
    calls = _run_returning(monkeypatch, json.dumps({"services": {}}))

    assert stack.build(["missing"]) == 2
    assert not [c for c in calls if len(c) >= 2 and c[1] == "build"]
    assert "unknown compose service(s): missing" in capsys.readouterr().err


def test_podman_build_success_keeps_build_output_off_the_terminal(fake_repo, monkeypatch, capsys):
    # A warm `fy up` used to scroll hundreds of layer/apt lines past the summaries that matter:
    # build output now streams to <state_dir>/build-<project>.log; the terminal keeps only the
    # echoed `+ podman build …` command and a one-time pointer at the log.
    import types

    cfg = json.dumps({"services": {"queue-worker": {"build": {"context": "/repo/data"}}}})
    build_kwargs: list[dict] = []

    def fake_run(cmd, **kw):
        if "config" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=cfg, stderr="")
        if len(cmd) >= 2 and cmd[1] == "build":
            build_kwargs.append(kw)
            kw["stdout"].write("STEP 1/8: hundreds of layer lines\n")
        return _FakeProc()

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    assert stack.up() == 0
    assert build_kwargs and build_kwargs[0]["stderr"] is stack.subprocess.STDOUT
    captured = capsys.readouterr()
    assert "hundreds of layer lines" not in captured.out + captured.err
    assert "+ podman build" in captured.err  # the command echo survives (thin-and-echoing rule)
    log_path = config.state_dir() / "build-tangible-podman.log"
    assert f"build output → {log_path}" in captured.err
    assert "hundreds of layer lines" in log_path.read_text()


def test_podman_build_failure_tails_the_log_and_names_it(fake_repo, monkeypatch, capsys):
    import types

    cfg = json.dumps({"services": {"queue-worker": {"build": {"context": "/repo/data"}}}})

    def fake_run(cmd, **kw):
        if "config" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=cfg, stderr="")
        if len(cmd) >= 2 and cmd[1] == "build":
            kw["stdout"].write("STEP 1/8: fine\nBOOM: no space left on device\n")
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        return _FakeProc()

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    assert stack.up() == 1
    err = capsys.readouterr().err
    assert "✗ build failed for queue-worker (exit 1)" in err
    assert "BOOM: no space left on device" in err  # the tail is shown inline
    assert f"full log: cat {config.state_dir() / 'build-tangible-podman.log'}" in err


def test_up_warns_last_when_a_capability_is_degraded(fake_repo, capture_run, monkeypatch, capsys):
    # `fy up` under a lapsed credential chain must not finish looking healthy: the supervisor's
    # published probed-and-failing capability (the claim `fy mode` renders as DEGRADED) prints
    # as the LAST line of the up summary.
    monkeypatch.setattr(devmode, "read", lambda *a, **k: {"mode": {"gcp": "sa", "github": "off"}})
    Path(os.environ["FOLDYARD_CAPABILITIES_FILE"]).write_text(
        json.dumps(
            {"": {"gcp": {"ok": False, "detail": "PAM lapsed — just gcp-elevate", "checked": "t"}}}
        )
    )
    assert stack.up() == 0
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last.startswith("⚠ gcp capability DEGRADED — PAM lapsed — just gcp-elevate")


def test_up_stays_quiet_when_capabilities_are_healthy(fake_repo, capture_run, monkeypatch, capsys):
    monkeypatch.setattr(devmode, "read", lambda *a, **k: {"mode": {"gcp": "sa", "github": "off"}})
    Path(os.environ["FOLDYARD_CAPABILITIES_FILE"]).write_text(
        json.dumps({"": {"gcp": {"ok": True, "detail": "chain ok", "checked": "t"}}})
    )
    assert stack.up() == 0
    assert "DEGRADED" not in capsys.readouterr().out


@pytest.fixture
def stackless_repo(fake_repo):
    """fake_repo with `[project].compose` UNSET — a box-only consumer. The compose.podman.yml
    the fixture wrote stays on disk on purpose: the stack is what the toml declares, and a
    stray file of the old default's name must not be read as one."""
    toml = fake_repo / "foldyard.toml"
    toml.write_text(toml.read_text().replace('compose = ["compose.podman.yml"]\n', ""))
    config.clear_caches()
    assert (fake_repo / "compose.podman.yml").exists()
    return fake_repo


def test_up_on_compose_less_project_starts_supervisor_and_points_at_box(
    stackless_repo, capture_run, capsys, monkeypatch
):
    # A box-only project still ensures the machine, but has nothing to `compose up` — succeed
    # and point at `foldyard box up` + where a stack gets set up, instead of failing on a path.
    started: list[bool] = []
    monkeypatch.setattr(supervisor, "ensure_background", lambda: started.append(True))

    assert stack.up() == 0
    assert _composes(capture_run) == []  # never invoked compose
    assert started == [True]
    out = capsys.readouterr().out
    assert "[project].compose" in out and "fy box up" in out and "fy docs quickstart" in out


# Every verb that acts on the compose stack, with the box counterpart its note points at.
_STACK_VERBS = [
    ("ps", lambda: stack.ps(), "fy box ps"),
    ("logs", lambda: stack.logs([]), None),
    ("shell", lambda: stack.shell(), "fy box shell"),
    ("build", lambda: stack.build([]), "fy box build"),
    ("down", lambda: stack.down(), "fy box down"),
    ("nuke", lambda: stack.nuke(), "fy box down"),
]


@pytest.mark.parametrize(
    ("verb", "run", "box_hint"), _STACK_VERBS, ids=[v[0] for v in _STACK_VERBS]
)
def test_stack_verbs_without_a_declared_stack_explain_and_exit_zero(
    stackless_repo, capture_run, capsys, verb, run, box_hint
):
    # `fy ps` on a box-only project used to hand podman-compose an invented `-f
    # compose.podman.yml` and exit 1 on its CRITICAL "missing files" — a scary non-failure an
    # agent then debugs. Now: say what the verb acts on, that this project declares no stack,
    # where the stack is set up — and touch NOTHING (no engine, no machine, no git).
    assert run() == 0
    assert capture_run == []
    out = capsys.readouterr().out
    assert f"`fy {verb}`" in out and "[project].compose" in out
    assert "fy docs quickstart" in out and "fy docs configuration" in out
    if box_hint:
        assert box_hint in out
    else:
        assert "fy box" not in out


@pytest.mark.parametrize(("verb", "run", "_"), _STACK_VERBS, ids=[v[0] for v in _STACK_VERBS])
def test_stack_verbs_with_a_declared_file_missing_abort_naming_it(
    fake_repo, capture_run, capsys, verb, run, _
):
    # Declared but gone (wrong branch, a rename): that IS an error — but foldyard's, naming the
    # file and the checkout it looked in, not the compose provider's "missing files" dump.
    (fake_repo / "compose.podman.yml").unlink()
    assert run() == 1
    assert _composes(capture_run) == []
    err = capsys.readouterr().err
    assert "compose.podman.yml" in err and str(fake_repo) in err and "[project].compose" in err


def test_declared_file_is_checked_in_the_explicit_worktree(
    fake_repo, capture_run, capsys, monkeypatch
):
    # WORKTREE=feat binds the stack to the sibling checkout, and that is where resolve() joins
    # the `-f` paths — so the preflight must look there too. Checking config.repo_root() (cwd /
    # FOLDYARD_REPO = main) passed on main's copy and handed the provider a path under the
    # worktree that wasn't there.
    wt = Path(f"{fake_repo}-worktrees") / "feat"
    wt.mkdir(parents=True)  # no compose.podman.yml in it; main's is still on disk
    monkeypatch.setenv("WORKTREE", "feat")
    monkeypatch.setattr(stack, "_offset", lambda name: 7)
    assert stack.ps() == 1
    assert _composes(capture_run) == []
    err = capsys.readouterr().err
    assert "compose.podman.yml" in err and str(wt) in err and str(fake_repo) + "." not in err
    # And the converse: the worktree carries the file, main doesn't — the worktree's is the one.
    (wt / "compose.podman.yml").write_text("services: {}\n")
    (fake_repo / "compose.podman.yml").unlink()
    assert stack.ps() == 0
    assert str(wt / "compose.podman.yml") in _composes(capture_run)[-1]


def test_up_with_a_declared_file_missing_aborts_naming_it(fake_repo, capture_run, capsys):
    (fake_repo / "compose.podman.yml").unlink()
    assert stack.up() == 1
    assert _composes(capture_run) == []
    err = capsys.readouterr().err
    assert "compose.podman.yml" in err and "[project].compose" in err


def test_nuke_main_removes_volumes(fake_repo, capture_run):
    assert stack.nuke() == 0  # no WORKTREE → main teardown
    assert _composes(capture_run)[-1][-3:] == ["down", "-v", "--remove-orphans"]


def test_nuke_worktree_reaps_orphans(fake_repo, capture_run, monkeypatch):
    # Same reason as `down`: a profile-gated container from an earlier posture (the metadata
    # emulator after gcp=sa→off) isn't in the rendered config, so a plain `down` strands it —
    # on worktree REMOVAL it then outlives the checkout (observed 2026-09-15).
    _worktree(fake_repo, "feat")
    monkeypatch.setenv("WORKTREE", "feat")
    monkeypatch.setattr(stack, "_offset", lambda name: 7)
    assert stack.nuke() == 0
    down = next(c for c in _composes(capture_run) if "down" in c)
    assert down[-2:] == ["down", "--remove-orphans"]


def test_up_ensures_external_network_before_compose(fake_repo, capture_run, monkeypatch):
    # external_network consumer: compose declares `{project}_default` external and never creates
    # it — `up` must, before compose attaches anything. (The create-when-missing branch of the
    # shared ensure_network is exercised in test_box.py; the fake engine here answers every
    # probe with success, so the inspect short-circuits.)
    monkeypatch.setattr(config, "external_network", lambda: True)
    assert stack.up() == 0
    probe = next(i for i, c in enumerate(capture_run) if c[:3] == ["podman", "network", "inspect"])
    assert capture_run[probe][3] == "tangible-podman_default"
    up_idx = next(i for i, c in enumerate(capture_run) if "compose" in c and "up" in c)
    assert probe < up_idx


def test_up_skips_network_ensure_when_compose_owns_it(fake_repo, capture_run):
    # Knob off (the default, and every consumer whose compose stack owns its networks):
    # up never touches `network` — compose creates it exactly as before.
    assert stack.up() == 0
    assert not any("network" in c for c in capture_run)


def test_shellenv_ensures_external_network(fake_repo, capture_run, monkeypatch, capsys):
    # Raw-compose recipes (`"${COMPOSE[@]}" up …`, e.g. a cold `just e2e`) rely on shellenv's
    # shared setup — so it ensures the external network just like it ensures the machine.
    monkeypatch.setattr(config, "external_network", lambda: True)
    assert stack.shellenv() == 0
    assert any(c[:3] == ["podman", "network", "inspect"] for c in capture_run)
    assert "COMPOSE=(" in capsys.readouterr().out  # still emits the recipe env


def test_nuke_removes_external_network(fake_repo, capture_run, monkeypatch):
    # nuke owns the external network's removal (compose can't remove what it doesn't own).
    # Best-effort: while the dev box still holds it the rm fails quietly and it stays.
    monkeypatch.setattr(config, "external_network", lambda: True)
    assert stack.nuke() == 0
    down_idx = next(i for i, c in enumerate(capture_run) if "compose" in c and "down" in c)
    rm_idx = next(
        i
        for i, c in enumerate(capture_run)
        if c[:3] == ["podman", "network", "rm"] and c[3] == "tangible-podman_default"
    )
    assert down_idx < rm_idx


# ── disk headroom + the pre-build reclaim ─────────────────────────────────────────────


def _headroom_run(free_gib: float, total_gib: float = 90.0):
    """A subprocess.run stub answering `podman info` with a store of the given size, recording
    every command. Returns (recorder, fake_run)."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        proc = _FakeProc()
        if cmd[:2] == ["podman", "info"]:
            total, free = int(total_gib * 1024**3), int(free_gib * 1024**3)
            proc.stdout = json.dumps(
                {"store": {"graphRootAllocated": total, "graphRootUsed": total - free}}
            )
        return proc

    return calls, fake_run


def test_reclaim_does_nothing_while_the_store_has_room(fake_repo, monkeypatch):
    # The whole design is threshold-driven: a healthy store must see NO destructive verb, so a
    # routine `fy up` can never be the thing that deletes an image.
    calls, fake_run = _headroom_run(free_gib=40.0)
    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    stack.reclaim(stack.resolve())
    assert not [c for c in calls if "prune" in c or "rmi" in c]


def test_reclaim_prunes_only_aged_dangling_images(fake_repo, monkeypatch):
    # Two invariants that make the sweep safe on a SHARED box:
    #  * `until=` — a build in flight commits untagged, childless (= dangling) layers, and an
    #    unguarded prune would delete another session's build out from under it;
    #  * never `-a` — that removes unused TAGGED images (the base images) too, and the layer
    #    cache lives in parent images that plain `prune` structurally cannot reach.
    calls, fake_run = _headroom_run(free_gib=2.0)
    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    monkeypatch.setattr(stack, "_live_projects", lambda ctx: None)  # sweep 2 off for this test
    stack.reclaim(stack.resolve())
    prune = next(c for c in calls if "prune" in c)
    assert prune == ["podman", "image", "prune", "-f", "--filter", "until=24h"]
    assert "-a" not in prune and "--all" not in prune


def test_reclaim_triggers_on_a_low_fraction_of_a_large_store(fake_repo, monkeypatch):
    # 12 GiB free is far above the absolute floor but only 6% of the disk — a single app-image
    # build is bigger than that, so the fraction has to be able to trigger on its own.
    calls, fake_run = _headroom_run(free_gib=12.0, total_gib=200.0)
    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    monkeypatch.setattr(stack, "_live_projects", lambda ctx: None)
    stack.reclaim(stack.resolve())
    assert [c for c in calls if "prune" in c]


def test_reclaim_warns_instead_of_ticking_when_the_store_stays_low(fake_repo, monkeypatch, capsys):
    # `✓ reclaimed 0.0 GiB` on a store that is still low is a tick on a failure (seen live: every
    # dangling image was younger than the guard, the build died on ENOSPC anyway). Say it is
    # still low and name the sweeps this deliberately leaves to a human.
    _, fake_run = _headroom_run(free_gib=2.0)
    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    monkeypatch.setattr(stack, "_live_projects", lambda ctx: None)
    stack.reclaim(stack.resolve())
    out = capsys.readouterr().out
    assert "✓" not in out
    assert "still low" in out
    assert "podman system df" in out and "podman image prune -a" in out


def test_reclaim_now_sweeps_regardless_of_headroom(fake_repo, monkeypatch):
    # The manual verb exists for the emergency — a store that is full NOW, with a box you can't
    # afford to bounce — so it must not be gated on the same threshold that already failed to
    # help. Same sweeps as `up`'s, just unconditional.
    calls, fake_run = _headroom_run(free_gib=40.0)
    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    monkeypatch.setattr(stack, "_live_projects", lambda ctx: None)
    assert stack.reclaim_now() == 0
    assert [c for c in calls if c[:3] == ["podman", "image", "prune"]]


def test_forced_reclaim_reports_headroom_even_when_the_before_probe_failed(
    fake_repo, monkeypatch, capsys
):
    # The two probes are independent: `fy reclaim` runs regardless of the first, so a store the
    # first probe couldn't read but the second can must still get its state said.
    _, headroom = _headroom_run(free_gib=40.0)
    probes = 0

    def fake_run(cmd, **kw):
        nonlocal probes
        proc = headroom(cmd, **kw)
        if cmd[:2] == ["podman", "info"]:
            probes += 1
            if probes == 1:
                proc = _FakeProc()
                proc.returncode = 1
        return proc

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    monkeypatch.setattr(stack, "_live_projects", lambda ctx: None)
    stack.reclaim(stack.resolve(), force=True)
    out = capsys.readouterr().out
    assert "headroom unknown" in out  # the banner, before the sweeps
    assert "✓" in out and "40.0 GiB" in out  # the after-state, from the second probe


# ── the project's own [reclaim] script ───────────────────────────────────────────────


def _box_runs(monkeypatch, *, box_running: bool, free_gib: float = 2.0):
    """`_headroom_run` plus an answer for the dev-box liveness probe (`ps -q -f name=…`)."""
    calls, headroom = _headroom_run(free_gib=free_gib)

    def fake_run(cmd, **kw):
        proc = headroom(cmd, **kw)
        if cmd[:2] == ["podman", "ps"] and any(a.startswith("name=") for a in cmd):
            proc.stdout = "abc123\n" if box_running else ""
        return proc

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    monkeypatch.setattr(stack, "_live_projects", lambda ctx: None)
    return calls


def test_reclaim_runs_the_project_script_in_the_box(fake_repo, monkeypatch):
    # What ELSE eats the store is the consumer's business — pnpm/uv caches, e2e artefacts, a
    # buildx state volume — and lives in volumes only the box mounts, with tools only the box has.
    # foldyard owns WHEN (its threshold) and the engine-level sweeps; the script owns the rest.
    monkeypatch.setattr(config, "reclaim_script", lambda: "dev-stack/reclaim.sh")
    calls = _box_runs(monkeypatch, box_running=True)
    stack.reclaim(stack.resolve())
    hook = next(c for c in calls if c[:2] == ["podman", "exec"])
    assert "tangible-podman-devbox" in hook
    assert hook[hook.index("-w") + 1] == str(fake_repo)  # the checkout, not the caller's cwd
    assert hook[-1].endswith("sh dev-stack/reclaim.sh")
    # After the engine sweeps, not before: a script that fails must not cost them.
    assert calls.index(hook) > calls.index(next(c for c in calls if "prune" in c))


def test_reclaim_skips_the_project_script_when_the_box_is_down(fake_repo, monkeypatch, capsys):
    monkeypatch.setattr(config, "reclaim_script", lambda: "dev-stack/reclaim.sh")
    calls = _box_runs(monkeypatch, box_running=False)
    stack.reclaim(stack.resolve())
    assert not [c for c in calls if c[:2] == ["podman", "exec"]]
    assert "reclaim.sh" in capsys.readouterr().out  # said, not silent


def test_reclaim_runs_the_project_script_directly_inside_the_box(fake_repo, monkeypatch):
    # In-box `fy reclaim` (the emergency path) is already where the volumes are mounted.
    monkeypatch.setenv("IN_DEVBOX", "1")
    monkeypatch.setattr(config, "reclaim_script", lambda: "dev-stack/reclaim.sh")
    calls = _box_runs(monkeypatch, box_running=True)
    stack.reclaim(stack.resolve())
    assert ["sh", "dev-stack/reclaim.sh"] in calls
    assert not [c for c in calls if c[:2] == ["podman", "exec"]]


def test_reclaim_without_a_script_runs_no_hook(fake_repo, monkeypatch):
    monkeypatch.setattr(config, "reclaim_script", lambda: None)
    calls = _box_runs(monkeypatch, box_running=True)
    stack.reclaim(stack.resolve())
    assert not [c for c in calls if c[:2] == ["podman", "exec"] or c[:1] == ["sh"]]


# ── images a build superseded are removed once nothing runs them ─────────────────────


def _build_then_up_runs(
    monkeypatch,
    config_yaml: str,
    ids: dict[str, list[str]],
    tags: dict[str, list[str]] | None = None,
):
    """Record commands; answer `compose config` with the given services and `image inspect
    <name>` with the next id in ``ids[name]`` (the last repeats) — the image before and after
    the build. The RepoTags inspect of an id answers ``tags[id]`` (untagged by default)."""
    import types

    calls: list[list[str]] = []
    remaining = {k: list(v) for k, v in ids.items()}

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "config" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=config_yaml, stderr="")
        if cmd[:3] == ["podman", "image", "inspect"] and "{{json .RepoTags}}" in cmd:
            out = json.dumps((tags or {}).get(cmd[-1], []))
            return types.SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")
        if cmd[:3] == ["podman", "image", "inspect"]:
            seq = remaining.get(cmd[-1], [])
            out = (seq.pop(0) if len(seq) > 1 else (seq[0] if seq else "")) + "\n"
            return types.SimpleNamespace(returncode=0 if out.strip() else 1, stdout=out, stderr="")
        return _FakeProc()

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    return calls


_ONE_BUILD_SERVICE = json.dumps({"services": {"app": {"build": {"context": "/repo/app"}}}})


def test_up_removes_the_image_its_build_superseded(fake_repo, monkeypatch):
    # A rebuild retags `<project>_app`; the image it replaced is untagged, childless and OURS —
    # provably not another session's build in flight, which is what the age guard on the
    # dangling sweep exists for. So it needs no guard: remove it once `up` has moved the
    # containers off it. Without this, every `fy up` that rebuilds a 6 GB image leaves 6 GB
    # behind, all younger than any age guard exactly when the next build needs the space.
    calls = _build_then_up_runs(
        monkeypatch,
        _ONE_BUILD_SERVICE,
        {"tangible-podman_app": ["sha256:old", "sha256:new"]},
    )
    assert stack.up() == 0
    rmi = next(c for c in calls if c[:2] == ["podman", "rmi"])
    assert rmi == ["podman", "rmi", "sha256:old"]  # no --force: still-referenced ⇒ left alone
    up_idx = next(i for i, c in enumerate(calls) if "compose" in c and "up" in c)
    assert calls.index(rmi) > up_idx  # after the containers were recreated onto the new image


def test_up_keeps_an_image_the_build_left_unchanged(fake_repo, monkeypatch):
    # A fully cached build yields the same id — nothing was superseded.
    calls = _build_then_up_runs(
        monkeypatch, _ONE_BUILD_SERVICE, {"tangible-podman_app": ["sha256:same"]}
    )
    assert stack.up() == 0
    assert not [c for c in calls if c[:2] == ["podman", "rmi"]]


_TWO_SERVICES_ONE_SPEC = json.dumps(
    {
        "services": {
            "app": {"build": {"context": "/repo/app"}},
            "worker": {"build": {"context": "/repo/app"}, "profiles": ["jobs"]},
        }
    }
)


def test_up_keeps_a_superseded_id_another_tag_still_names(fake_repo, monkeypatch):
    # `app` and `worker` share one build spec, so one id carries both tags. An `up` without the
    # `jobs` profile rebuilds only `app`: the old id is superseded FOR THAT TAG but is still
    # `<project>_worker`'s image — and with no worker container holding it, a bare `rmi <id>`
    # would take it (the engine only refuses ids in use, not ids merely tagged). Skip it.
    inactive = json.dumps({"services": {"app": {"build": {"context": "/repo/app"}}}})
    calls = _build_then_up_runs(
        monkeypatch,
        inactive,
        {"tangible-podman_app": ["sha256:shared", "sha256:new"]},
        tags={"sha256:shared": ["localhost/tangible-podman_worker:latest"]},
    )
    assert stack.up() == 0
    assert not [c for c in calls if c[:2] == ["podman", "rmi"]]
    # …whereas once both tags have moved off it, it goes.
    calls = _build_then_up_runs(
        monkeypatch,
        _TWO_SERVICES_ONE_SPEC,
        {
            "tangible-podman_app": ["sha256:shared", "sha256:new"],
            "tangible-podman_worker": ["sha256:shared", "sha256:new"],
        },
    )
    assert stack.up() == 0
    assert [c for c in calls if c[:2] == ["podman", "rmi"]] == [["podman", "rmi", "sha256:shared"]]


def test_up_keeps_a_superseded_id_whose_tags_are_unreadable(fake_repo, monkeypatch):
    # Fail closed: tags that can't be read count as "still referenced".
    import types

    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "config" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=_ONE_BUILD_SERVICE, stderr="")
        if cmd[:3] == ["podman", "image", "inspect"] and "{{json .RepoTags}}" in cmd:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        if cmd[:3] == ["podman", "image", "inspect"]:
            seen = [c for c in calls if c == cmd]
            out = "sha256:old\n" if len(seen) == 1 else "sha256:new\n"
            return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
        return _FakeProc()

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    assert stack.up() == 0
    assert not [c for c in calls if c[:2] == ["podman", "rmi"]]


def test_up_keeps_the_old_image_when_the_build_fails(fake_repo, monkeypatch):
    import types

    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "config" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=_ONE_BUILD_SERVICE, stderr="")
        if cmd[:3] == ["podman", "image", "inspect"]:
            return types.SimpleNamespace(returncode=0, stdout="sha256:old\n", stderr="")
        proc = _FakeProc()
        if len(cmd) >= 2 and cmd[1] == "build":
            proc = types.SimpleNamespace(returncode=1, stdout="", stderr="")
        return proc

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    assert stack.up() != 0
    assert not [c for c in calls if c[:2] == ["podman", "rmi"]]


def test_reclaim_is_a_no_op_when_the_disk_figure_is_unknown(fake_repo, monkeypatch):
    # docker (no equivalent field), a stopped VM, a malformed payload — all must read as "no
    # finding, change nothing" rather than as pressure.
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        proc = _FakeProc()
        proc.returncode = 1 if cmd[:2] == ["podman", "info"] else 0
        return proc

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    assert stack.disk_headroom() is None
    stack.reclaim(stack.resolve())
    assert not [c for c in calls if "prune" in c or "rmi" in c]


def test_orphan_images_keep_live_projects_and_ambiguous_names(fake_repo, monkeypatch):
    # The parsing traps, all in one store: BOTH separators for the main project, a live
    # worktree, a removed one, a removed worktree whose name is a PREFIX of a live one, an
    # image whose name is ambiguous between "main project + e2e-app" and "worktree e2e +
    # app", and a third-party image. Only the genuinely dead worktrees' images may go.
    services = {"app", "e2e-app", "postgres"}
    live = {"tangible-podman", "tangible-podman-fy-gh-bot"}
    listing = [
        "localhost/tangible-podman_app:latest",  # main, podman-compose spelling
        "localhost/tangible-podman-postgres:latest",  # main, docker-compose spelling
        "localhost/tangible-podman-e2e-app:latest",  # ambiguous — also reads as worktree 'e2e'
        "localhost/tangible-podman-fy-gh-bot_app:latest",  # live worktree
        "localhost/tangible-podman-fy_app:latest",  # removed worktree, a PREFIX of the live one
        "localhost/tangible-podman-page-guides-postgres:latest",  # removed, docker spelling
        "docker.io/library/postgres:16",  # not ours at all
        "<none>:<none>",  # dangling — sweep 1's business, not this one's
    ]

    def fake_run(cmd, **kw):
        proc = _FakeProc()
        if cmd[1:2] == ["images"]:
            proc.stdout = "\n".join(listing)
        return proc

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    assert stack._orphan_project_images(stack.resolve(), services, live) == [
        "localhost/tangible-podman-fy_app:latest",
        "localhost/tangible-podman-page-guides-postgres:latest",
    ]


def test_orphan_images_sweep_nothing_without_a_service_list(fake_repo, monkeypatch):
    # An unrenderable compose config leaves every name undecomposable. Fail-safe: keep
    # everything (the alternative is guessing where a project name ends).
    monkeypatch.setattr(stack.subprocess, "run", lambda cmd, **kw: _FakeProc())
    assert stack._orphan_project_images(stack.resolve(), set(), {"tangible-podman"}) == []


def test_live_projects_is_none_when_the_worktree_list_is_unreadable(fake_repo, monkeypatch):
    # `None` (not an empty set) — an unreadable worktree list must never read as "every
    # worktree project is orphaned", which would sweep live worktrees' images.
    def fake_run(cmd, **kw):
        proc = _FakeProc()
        if cmd[:2] == ["git", "-C"]:
            proc.returncode = 1
        return proc

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    assert stack._live_projects(stack.resolve()) is None


def test_live_projects_lists_the_main_project_and_every_registered_worktree(fake_repo, monkeypatch):
    def fake_run(cmd, **kw):
        proc = _FakeProc()
        if cmd[:2] == ["git", "-C"]:
            proc.stdout = (
                f"worktree {fake_repo}\nHEAD abc\nbranch refs/heads/main\n\n"
                f"worktree {fake_repo}-worktrees/fy-gh-bot\nHEAD def\n"
            )
        return proc

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    assert stack._live_projects(stack.resolve()) == {
        "tangible-podman",
        "tangible-podman-fy-gh-bot",
    }


def test_live_projects_flattens_a_nested_worktree_path_like_the_project_name_does(
    fake_repo, monkeypatch
):
    # `_context` builds PODMAN_PROJECT as `{prefix}-{worktree-path-with-/-as-}`, so a worktree at
    # <root>/feat/foo owns `tangible-podman-feat-foo`. Deriving the name from the BASENAME would
    # put `tangible-podman-foo` in the live set instead, leaving a live worktree's images
    # unclaimed — and the orphan sweep would then delete them.
    def fake_run(cmd, **kw):
        proc = _FakeProc()
        if cmd[:2] == ["git", "-C"]:
            proc.stdout = (
                f"worktree {fake_repo}\nHEAD abc\n\n"
                f"worktree {fake_repo}-worktrees/feat/foo\nHEAD def\n"
            )
        return proc

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    assert stack._live_projects(stack.resolve()) == {
        "tangible-podman",
        "tangible-podman-feat-foo",
    }


def test_up_reclaims_before_it_builds(fake_repo, capture_run, monkeypatch):
    # Ordering is the whole point: reclaiming after the build cannot stop the build from
    # running out of space.
    seen: list[str] = []
    monkeypatch.setattr(stack, "reclaim", lambda ctx, **kw: seen.append("reclaim"))
    monkeypatch.setattr(stack, "_build", lambda ctx, **kw: (seen.append("build"), 0)[1])
    monkeypatch.setattr(stack, "_print_banner", lambda env: None)
    monkeypatch.setattr(supervisor, "ensure_background", lambda: None)
    stack.up()
    assert seen == ["reclaim", "build"]


def test_remove_devbox_volumes_drops_only_this_boxes_volumes(fake_repo, monkeypatch):
    # `box down` keeps the per-box shadow volumes so restarts stay warm; worktree removal
    # drops them via this helper (nuke's `{project}_` compose filter can't match their
    # hyphenated names). The engine's `--filter name=` is a SUBSTRING match, so a longer
    # sibling project's volumes can appear in the listing — only exact-prefix names go.
    wt = Path(f"{fake_repo}-worktrees") / "feat"
    wt.mkdir(parents=True)
    monkeypatch.setenv("WORKTREE", "feat")
    monkeypatch.setattr(stack, "_offset", lambda name: 7)
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        proc = _FakeProc()
        if cmd[1:3] == ["volume", "ls"]:
            proc.stdout = (
                "tangible-podman-feat-devbox-shadow-node_modules\n"
                "tangible-podman-feat-extra-devbox-shadow-node_modules\n"
            )
        return proc

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    assert stack.remove_devbox_volumes() == 1  # returns how many were removed
    rm = next(c for c in calls if c[1:3] == ["volume", "rm"])
    assert rm == ["podman", "volume", "rm", "tangible-podman-feat-devbox-shadow-node_modules"]


def test_remove_devbox_volumes_project_override_skips_worktree_resolve(fake_repo, monkeypatch):
    # The checkout-already-gone retry path can't resolve() the worktree (its dir is deleted) —
    # an explicit project= resolves the MAIN context for env and pins the volume prefix.
    monkeypatch.setenv("WORKTREE", "gone")  # dir deliberately NOT created
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        proc = _FakeProc()
        if cmd[1:3] == ["volume", "ls"]:
            proc.stdout = "tangible-podman-gone-devbox-shadow-node_modules\n"
        return proc

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    assert stack.remove_devbox_volumes(project="tangible-podman-gone") == 1
    rm = next(c for c in calls if c[1:3] == ["volume", "rm"])
    assert rm == ["podman", "volume", "rm", "tangible-podman-gone-devbox-shadow-node_modules"]


def test_nuke_archives_transcripts_first(fake_repo, capture_run, monkeypatch):
    from foldyard import transcripts

    seen = []
    monkeypatch.setattr(transcripts, "sync_current", lambda env, **k: (seen.append(env), 0)[1])
    assert stack.nuke() == 0
    assert seen  # transcripts archived before volumes are torn down


# ── live stack posture reconcile on a posture change (per-consumer-registry-plan.md 2.4) ─


def _sig(profiles: str = "", overlays: list[str] | None = None) -> dict:
    """A minimal devmode.posture_signature-shaped dict for driving reconcile_posture."""
    return {"env": {"COMPOSE_PROFILES": profiles}, "overlays": overlays or []}


class _StreamCalls(list):
    """The recorded run_stream commands, with a mutable ``rc`` to fake a compose failure."""

    def __init__(self) -> None:
        super().__init__()
        self.rc = {"value": 0}


@pytest.fixture
def capture_stream(monkeypatch):
    """Record the streamed compose command the reconcile runs (devmode.run_stream — the up no
    longer goes through subprocess.run) and fake its output + exit code. ALSO stubs
    stack.subprocess.run: reconcile_posture's engine probes (the foreign-container sweep,
    ``_running_extra_profiles``) would otherwise run for REAL — which is how an escaped
    `podman rm -f` deleted the dev box's entire live stack (see the fake_repo DOCKER_HOST
    comment). Yields the recorded command list; set ``capture_stream.rc["value"]`` to fake a
    failure."""
    calls = _StreamCalls()

    def fake_stream(cmd, on_line, **_kw):
        calls.append(cmd)
        on_line("Recreating queue-worker")
        on_line("WARN[0000] SIM_PORT not set")
        return calls.rc["value"]

    monkeypatch.setattr(stack.devmode, "run_stream", fake_stream)
    monkeypatch.setattr(stack.subprocess, "run", lambda cmd, **kw: _FakeProc())
    return calls


def test_reconcile_noop_when_signature_unchanged(fake_repo, capture_stream):
    assert stack.reconcile_posture(_sig("metadata"), _sig("metadata")) is True
    assert capture_stream == []  # no change → never touches compose


def test_reconcile_fires_on_overlay_only_change(fake_repo, capture_stream, monkeypatch):
    # The trigger is the FULL posture signature, not COMPOSE_PROFILES alone: an overlay/env-only
    # change (llm=off→record adds compose.llm.yml with no profile change) must reconcile too —
    # profile-equality used to skip it, leaving running containers on the old env.
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(stack, "_stack_is_up", lambda ctx, ignore_services=frozenset(): True)
    ok = stack.reconcile_posture(_sig(""), _sig("", overlays=["compose.llm.yml"]))
    assert ok is True
    assert capture_stream and capture_stream[-1][-2:] == ["up", "-d"]


def test_reconcile_noop_when_stack_not_up(fake_repo, capture_stream, monkeypatch):
    # Mac-side (reconcile is Mac-only); the stack is NOT up → never START it on a posture change.
    # fake_repo's mocked mode is gcp=off, so no posture service is wanted either — the stack-down
    # branch converges an empty set and touches nothing.
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(stack, "_stack_is_up", lambda ctx, ignore_services=frozenset(): False)
    assert stack.reconcile_posture(_sig(""), _sig("metadata")) is True
    assert capture_stream == []  # guardrail: never START a stack on a posture change


def test_reconcile_noop_when_stackless(stackless_repo, capture_stream, monkeypatch):
    # No declared stack ⇒ nothing to reconcile (and no engine probe at all).
    monkeypatch.setattr(config, "in_box", lambda: False)
    assert stack.reconcile_posture(_sig(""), _sig("metadata")) is True
    assert capture_stream == []


def test_reconcile_brings_up_when_changed_and_up(fake_repo, capture_stream, monkeypatch):
    # fake_repo's mocked mode is gcp=off, so stage_assets is a no-op; the reconcile just re-applies
    # the posture to the already-up stack with a no-build compose up. Mac-side (in_box False).
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(stack, "_stack_is_up", lambda ctx, ignore_services=frozenset(): True)
    assert stack.reconcile_posture(_sig(""), _sig("metadata")) is True
    assert capture_stream and capture_stream[-1][-2:] == ["up", "-d"]
    # No-build contract: a posture toggle must NEVER rebuild — assert it explicitly, not just via
    # the trailing args, so a stray `--build` creeping into any up command is caught.
    assert not any("--build" in c for c in capture_stream)
    # NEVER `--remove-orphans` on the reconcile up: the bundled podman-compose implements it by
    # tearing down EVERY project container (its internal recreate `down` inherits the flag and
    # ignores service scoping) — a 2026-08-05 `fy mode gcp=sa→user` removed a whole running
    # stack that way. Orphans are reaped by foldyard's own targeted sweep instead.
    assert not any("--remove-orphans" in c for c in capture_stream)


def test_reconcile_streams_output_through_sink(fake_repo, capture_stream, monkeypatch):
    # The reconcile output routes through the sink (never the terminal fds) — the fix for raw
    # compose output bleeding over the Textual TUI — and reports success to the caller.
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(stack, "_stack_is_up", lambda ctx, ignore_services=frozenset(): True)
    lines: list[str] = []
    assert stack.reconcile_posture(_sig(""), _sig("metadata"), sink=lines.append) is True
    assert any("Recreating queue-worker" in ln for ln in lines)
    assert any("WARN" in ln for ln in lines)
    assert any(ln.startswith("+ ") for ln in lines)  # the echoed command line


def test_reconcile_reports_compose_failure(fake_repo, capture_stream, monkeypatch):
    # A compose run that FAILS must not be reported as success — the TUI used to notify
    # '✓ stack reconciled' whatever the exit code was.
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(stack, "_stack_is_up", lambda ctx, ignore_services=frozenset(): True)
    capture_stream.rc["value"] = 17
    lines: list[str] = []
    assert stack.reconcile_posture(_sig(""), _sig("metadata"), sink=lines.append) is False
    assert any("✗ reconcile failed" in ln and "17" in ln for ln in lines)


def _host_log_lines() -> list[str]:
    path = config.supervisor_log_file()
    return path.read_text().splitlines() if path.exists() else []


def test_reconcile_outcome_lands_in_the_host_log(fake_repo, capture_stream, monkeypatch):
    # #33: the reconcile runs in the `fy mode` process, so its output only ever reached that
    # terminal — `fy host logs` had no trace of whether a mode change re-rendered the stack.
    # One stamped [mode] line per outcome, beside the supervisor's own lines.
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(stack, "_stack_is_up", lambda ctx, ignore_services=frozenset(): True)
    assert stack.reconcile_posture(_sig(""), _sig("metadata"), sink=lambda _l: None) is True
    (ok,) = _host_log_lines()
    assert "[mode]" in ok and "reconciled" in ok and "metadata" in ok
    assert ok.split(" ", 1)[0].startswith("20")  # the ISO date stamp the supervisor's tee uses

    capture_stream.rc["value"] = 17
    assert stack.reconcile_posture(_sig(""), _sig("metadata"), sink=lambda _l: None) is False
    failed = _host_log_lines()[-1]
    assert "[mode]" in failed and "FAILED" in failed and "17" in failed


def test_reconcile_on_a_down_stack_says_so_in_the_host_log(fake_repo, capture_stream, monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(stack, "_stack_is_up", lambda ctx, ignore_services=frozenset(): False)
    assert stack.reconcile_posture(_sig(""), _sig("metadata"), sink=lambda _l: None) is True
    (line,) = _host_log_lines()
    assert "[mode]" in line and " down — the mode applies on next `fy up`" in line


def test_reconcile_unions_running_extra_profiles(fake_repo, capture_stream, monkeypatch):
    # Profile-gated services a dev started OUTSIDE the mode system (just data-up) must be
    # re-rendered into the new posture too: their profile is unioned into the up.
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(stack, "_stack_is_up", lambda ctx, ignore_services=frozenset(): True)
    monkeypatch.setattr(stack, "_running_extra_profiles", lambda ctx, deadline=None: ["data"])
    assert stack.reconcile_posture(_sig(""), _sig("metadata")) is True
    cmd = capture_stream[-1]
    assert cmd[cmd.index("--profile") + 1] == "data"


def test_reconcile_targets_bound_worktree(fake_repo, capture_stream, monkeypatch):
    wt = Path(f"{fake_repo}-worktrees") / "feat"
    wt.mkdir(parents=True)
    (wt / "foldyard.toml").write_text(_FAKE_TOML)
    (wt / "compose.podman.yml").write_text("services: {}\n")
    monkeypatch.setattr(config, "in_box", lambda: False)
    seen: dict[str, str] = {}

    def stack_is_up(ctx, ignore_services=frozenset()):
        seen["project"] = ctx.project
        seen["worktree"] = ctx.worktree
        return True

    monkeypatch.setattr(stack, "_stack_is_up", stack_is_up)
    monkeypatch.setattr(stack, "_offset", lambda name: 7)
    cfg = config.resolve(worktree="feat", repo=wt)

    stack.reconcile_posture(_sig(""), _sig("metadata"), cfg=cfg)

    assert capture_stream  # the up ran
    assert seen == {"project": "tangible-podman-feat", "worktree": "feat"}


# ── the stack-down posture-service path (Plugin.mode_services) ─
# A posture flip must never start the heavy stack (the guardrail above) — but the tiny services
# a rung is ENFORCED by (the gcp metadata emulator) must materialize even when the stack is
# down: the 2026-08-07 gcp=user-in-a-fresh-worktree trap left `fy mode` reading as granted
# while the box couldn't mint a single token, because the worktree's emulator only ever
# existed after a full `fy up`.


def test_reconcile_stack_down_materializes_posture_services(fake_repo, capture_stream, monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(devmode, "read", lambda *a, **k: {"mode": {"gcp": "user", "github": "off"}})
    monkeypatch.setattr(stack, "_stack_is_up", lambda ctx, ignore_services=frozenset(): False)
    ok = stack.reconcile_posture(_sig(""), _sig("metadata"))
    assert ok is True
    # ONLY the declared posture service comes up — never the whole stack, never a build.
    cmd = capture_stream[-1]
    assert cmd[-3:] == ["up", "-d", "metadata-emulator"]
    assert not any("--build" in c for c in capture_stream)


def test_reconcile_stack_down_posture_service_failure_reported(
    fake_repo, capture_stream, monkeypatch
):
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(devmode, "read", lambda *a, **k: {"mode": {"gcp": "logs", "github": "off"}})
    monkeypatch.setattr(stack, "_stack_is_up", lambda ctx, ignore_services=frozenset(): False)
    capture_stream.rc["value"] = 9
    lines: list[str] = []
    assert stack.reconcile_posture(_sig(""), _sig("metadata"), sink=lines.append) is False
    assert any("mode service start failed" in ln and "9" in ln for ln in lines)


def test_reconcile_stack_down_reaps_dropped_posture_service(fake_repo, capture_stream, monkeypatch):
    # gcp → off with ONLY the emulator running (its stack no longer is): the flip reaps it — a
    # zero-secret posture must not keep an identity emulator around — and starts nothing.
    # fake_repo's mocked mode is gcp=off; the engine probes are scripted to show a lone running
    # emulator container.
    monkeypatch.setattr(config, "in_box", lambda: False)
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        proc = _FakeProc()
        if cmd[1] == "ps":
            proc.stdout = "eee\n"
        elif cmd[1] == "inspect":
            proc.stdout = "eee metadata-emulator\n"
        return proc

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    monkeypatch.setattr(stack, "_stack_is_up", lambda ctx, ignore_services=frozenset(): False)
    lines: list[str] = []
    assert stack.reconcile_posture(_sig("metadata"), _sig(""), sink=lines.append) is True
    assert ["podman", "rm", "-f", "eee"] in calls
    assert capture_stream == []  # nothing started
    assert any("level dropped" in ln and "metadata-emulator" in ln for ln in lines)


def test_stack_is_up_ignores_posture_services(fake_repo, monkeypatch):
    # A lone posture service (the emulator the reconcile itself started) must not read as "the
    # stack is up" — that would turn the NEXT posture flip into a full-stack `up -d`.
    def world(ps: str, inspect: str):
        def fake_run(cmd, **kw):
            proc = _FakeProc()
            if cmd[1] == "ps":
                proc.stdout = ps
            elif cmd[1] == "inspect":
                proc.stdout = inspect
            return proc

        monkeypatch.setattr(stack.subprocess, "run", fake_run)

    ctx = _fake_ctx(fake_repo, {})
    world("proj-metadata\n", "metadata-emulator\n")
    assert stack._stack_is_up(ctx, {"metadata-emulator"}) is False
    world("proj-metadata\nproj-app\n", "metadata-emulator\napp\n")
    assert stack._stack_is_up(ctx, {"metadata-emulator"}) is True
    # An unlabeled/manual project container fails safe toward NOT starting the heavy stack.
    world("proj-metadata\n", "")
    assert stack._stack_is_up(ctx, {"metadata-emulator"}) is False
    # A failed/short inspect probe has the same fail-safe answer.
    world("proj-metadata\nproj-app\n", "metadata-emulator\n")
    assert stack._stack_is_up(ctx, {"metadata-emulator"}) is False
    # and without ignore_services the behavior is unchanged: any project container counts
    world("proj-metadata\n", "")
    assert stack._stack_is_up(ctx) is True


# ── the targeted orphan sweep (the reconcile's --remove-orphans replacement) ─
# `up --remove-orphans` is BANNED on the reconcile: the bundled podman-compose implements it
# via an internal down that removes every project container (see _remove_orphan_containers's
# docstring). These pin the replacement's exact engine sequence and its fail-safe edges.


def _orphan_world(monkeypatch, *, services: str, ps: str, inspect: str):
    """Script the sweep's three probes (compose config --services, engine ps, engine inspect)
    and record every command; returns the recorded call list."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        proc = _FakeProc()
        if "config" in cmd and "--services" in cmd:
            proc.stdout = services
        elif cmd[1] == "ps":
            proc.stdout = ps
        elif cmd[1] == "inspect":
            proc.stdout = inspect
        return proc

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    return calls


def test_orphan_sweep_removes_only_departed_services(fake_repo, monkeypatch):
    calls = _orphan_world(
        monkeypatch,
        services="app\npostgres\n",
        ps="aaa\nbbb\nccc\n",
        inspect="aaa app\nbbb metadata\nccc postgres\n",
    )
    lines: list[str] = []
    stack._remove_orphan_containers(_fake_ctx(fake_repo, {}), lines.append)
    rms = [c for c in calls if c[1] == "rm"]
    assert rms == [["podman", "rm", "-f", "bbb"]]  # ONLY the departed service's container
    assert any("metadata" in ln for ln in lines)  # named in the sweep announcement


def test_orphan_sweep_fails_safe_on_empty_render_and_unlabeled_containers(fake_repo, monkeypatch):
    # A failed/empty `config --services` render must remove NOTHING — an over-eager sweep here
    # is exactly the podman-compose bug this helper replaces.
    calls = _orphan_world(monkeypatch, services="", ps="aaa\n", inspect="aaa metadata\n")
    stack._remove_orphan_containers(_fake_ctx(fake_repo, {}), lambda _ln: None)
    assert not any(c[1] == "rm" for c in calls)
    # Containers with no service label (or a `<no value>` template render) are never orphans.
    calls = _orphan_world(
        monkeypatch, services="app\n", ps="aaa\nbbb\n", inspect="aaa \nbbb <no value>\n"
    )
    stack._remove_orphan_containers(_fake_ctx(fake_repo, {}), lambda _ln: None)
    assert not any(c[1] == "rm" for c in calls)


# ── running-profile discovery (the `data` workers must survive posture reconciles) ─


def test_compose_ps_services_parses_ndjson_and_array():
    ndjson = '{"Service":"queue-worker"}\n{"Service":"graph-api"}\nnot-json\n'
    assert stack._compose_ps_services(ndjson) == {"queue-worker", "graph-api"}
    array = '[{"Service":"app"},{"Service":""},{"Name":"x"}]'
    assert stack._compose_ps_services(array) == {"app"}
    assert stack._compose_ps_services("") == set()


def test_compose_ps_services_reads_podman_compose_rows():
    # #33: the bundled podman-compose's `ps --format json` is `podman ps -a --format json` —
    # no `Service` key (the service is a label) and exited containers included. Reading only
    # `Service` made every running profile invisible, so a posture reconcile never re-rendered
    # the `data` workers.
    rows = [
        {"Labels": {"com.docker.compose.service": "queue-worker"}, "State": "running"},
        {"Labels": {"com.docker.compose.service": "graph-api"}, "State": "running"},
        {"Labels": {"com.docker.compose.service": "e2e-app"}, "State": "exited"},
        {"Labels": None, "State": "running"},
    ]
    assert stack._compose_ps_services(json.dumps(rows)) == {"queue-worker", "graph-api"}


def test_compose_ps_services_cannot_read_garbage_as_nothing_running():
    # An empty set means "the engine says nothing runs"; output that parses to no row at all
    # says nothing, and must not skip a heal as if it did.
    assert stack._compose_ps_services("Error: something odd\n") is None
    assert stack._compose_ps_services("[]") == set()
    assert stack._compose_ps_services("") == set()


def _fake_ctx(fake_repo, env: dict[str, str]) -> stack.Context:
    return stack.Context(
        main=fake_repo,
        env=env,
        compose=["docker", "compose"],
        app="app",
        project="tangible-podman",
        worktree="",
    )


def test_profile_flags_reasserts_actives_alongside_extras(fake_repo):
    # Compose ignores COMPOSE_PROFILES the moment any --profile flag is passed, so extras must
    # carry the posture-derived actives with them; no extras → no flags (env alone rules).
    ctx = _fake_ctx(fake_repo, {"COMPOSE_PROFILES": "metadata,cloudsql"})
    assert stack._profile_flags(ctx, None) == []
    assert stack._profile_flags(ctx, ["data"]) == [
        "--profile",
        "metadata",
        "--profile",
        "cloudsql",
        "--profile",
        "data",
    ]
    assert stack._profile_flags(_fake_ctx(fake_repo, {}), ["data", "data"]) == [
        "--profile",
        "data",
    ]


def test_running_extra_profiles_discovers_from_compose(fake_repo, monkeypatch):
    # queue-worker (profile `data`) is running; profile `e2e` has no running service; the
    # active `metadata` profile is excluded. Fully generic — no profile names baked in.
    import types

    def fake_run(cmd, **_kw):
        out = ""
        if "ps" in cmd:
            out = '{"Service":"queue-worker"}\n{"Service":"app"}\n'
        if cmd[-1] == "--profiles":
            out = "data\ne2e\nmetadata\n"
        if cmd[-1] == "--services":
            if "data" in cmd:
                out = "app\npostgres\nqueue-worker\n"
            elif "e2e" in cmd:
                out = "app\npostgres\ne2e-app\n"
            else:
                out = "app\npostgres\n"
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(stack, "_run", fake_run)
    ctx = _fake_ctx(fake_repo, {"COMPOSE_PROFILES": "metadata"})
    assert stack._running_extra_profiles(ctx) == ["data"]


def test_up_unions_running_extra_profiles(fake_repo, capture_run, monkeypatch):
    # `fy up` after a posture change must re-render running profile-gated services too — the
    # user-visible symptom was data workers keeping their old identity across fy up.
    monkeypatch.setattr(stack, "_running_extra_profiles", lambda ctx, deadline=None: ["data"])
    assert stack.up() == 0
    up_calls = [c for c in _composes(capture_run) if "up" in c]
    assert up_calls and "--profile" in up_calls[0]
    assert up_calls[0][up_calls[0].index("--profile") + 1] == "data"


def test_compose_overlays_stacks_plugins_then_env_extra_and_filters_missing(monkeypatch, tmp_path):
    """stack._compose_overlays: plugin overlays first (existing files only), then manual
    FOLDYARD_COMPOSE_EXTRA entries (os.pathsep-joined) LAST so an explicit override wins; relative
    entries resolve against `base`, and any missing file is dropped."""
    a = tmp_path / "a.yml"
    a.write_text("services: {}\n")
    b = tmp_path / "b.yml"
    b.write_text("services: {}\n")
    (tmp_path / "rel.yml").write_text("services: {}\n")

    class _Reg:
        def compose_overlays(self, mode):
            return [str(a), str(tmp_path / "missing.yml")]  # one real, one bogus (dropped)

    monkeypatch.setattr("foldyard.plugins.registry", lambda *a, **k: _Reg())
    monkeypatch.setenv("FOLDYARD_COMPOSE_EXTRA", os.pathsep.join([str(b), "rel.yml"]))

    out = stack._compose_overlays({"gcp": "sa"}, base=tmp_path)
    assert out == [str(a), str(b), str(tmp_path / "rel.yml")]

    monkeypatch.delenv("FOLDYARD_COMPOSE_EXTRA")
    assert stack._compose_overlays({"gcp": "sa"}, base=tmp_path) == [str(a)]
