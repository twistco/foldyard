"""Opt-in LIVE end-to-end test: drive the real foldyard CLI against the example consumer on
a real container engine, and assert the stack actually serves a DB-backed request.

Unlike the golden/mocked tests, this runs the engine for real — `foldyard up` builds the api
image, pulls postgres, and the test queries the DB across the compose network. It is OPT-IN:
skipped unless FOLDYARD_E2E=1 AND an engine is reachable (it pulls images + builds, ~minutes).
Run it where foldyard has an engine — the dev box (the rootless machine socket), a nested
podman host (see docs/dev/testing.md), or CI's `lima-host-e2e` job, where the VM is created once before
pytest and its socket handed to THIS test's engine helpers as CONTAINER_HOST — never DOCKER_HOST,
which the CLI would take as "socket pre-exported" and skip `machine.ensure` — so `foldyard up`
here runs the REAL host path (machine ensure → adopt gate → supervisor → compose). It namespaces
under the example's own 'fyex' project, so it never collides with a host project's stack.

    FOLDYARD_E2E=1 just foldyard test -k e2e
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import e2e_adoptions

EXAMPLE = Path(__file__).resolve().parent.parent / "example"

# The autouse `scrubbed_box_session_env` fixture (conftest) strips IN_DEVBOX from every test's
# environment so the golden tests behave identically on a host, in CI and in a box. This test
# drives the REAL CLI as a subprocess, and that CLI must know where it is: in a box — the dev box,
# or CI's podman-client container — the launch gates stand down (no VM to manage, nothing host-side
# to adopt), and without the signature preflight refuses `up` over the default backend's missing
# limactl. Read it at import, before the fixture runs, and hand it back to the subprocess.
_IN_BOX = os.environ.get("IN_DEVBOX") == "1"
PROJECT = "fyex"  # the example's [project].prefix
NETWORK = f"{PROJECT}_default"  # compose's default network for that project
API_DB_URL = "http://api:8080/db"  # the api service (in-network) + the DB-wiring endpoint

# The host tier's checkout, under your home: ONE path for every example copy a host-tier run makes.
# The compose client runs in the VM (since 0.16.0), so the checkout must be one the VM mounts, and
# a VM mounts only the checkout it was created from and that checkout's worktrees root. With every
# copy at this path, and the CI jobs creating the first VM from it too (foldyard-e2e.yml, held to
# it by tests/test_e2e_workflow.py), whichever module (re)created the VM, it mounts the next one's.
# In a box (the live-e2e topology) the client runs there, and a per-test copy is fine.
HOST_TIER = "fy-e2e/host-tier"


def host_checkout() -> Path:
    """The host tier's one checkout (:data:`HOST_TIER`); its worktrees root is beside it,
    ``<checkout>-worktrees``, as foldyard derives it."""
    return Path.home() / HOST_TIER / "example"


def _emptied(d: Path) -> None:
    """``d`` there and empty, never removed: the VM shares the host tier's checkout and worktrees
    root by path, and a share whose directory was replaced reads stale. What can't be removed (a
    file a container made under an id that isn't yours) fails here by name, not later as a stray
    file in the next copy's first commit."""
    d.mkdir(parents=True, exist_ok=True)
    for child in d.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    left = sorted(str(c) for c in d.iterdir())
    assert not left, (
        f"couldn't empty {d} for a fresh example copy: {left[:5]} — most likely a file a "
        f"container made under an id that isn't yours. Remove it yourself (`sudo rm -rf {d}/*`), "
        "then run again."
    )


def fresh_example(dst: Path, extra_toml: str = "") -> Path:
    """``dst`` holding a fresh, committed git copy of example/ (foldyard's main_repo() shells
    git), with ``extra_toml`` appended to its foldyard.toml. ``dst`` and its worktrees root are
    emptied first, in place (:func:`_emptied`), and on your computer so is what it keeps by that
    path (its adoptions, its worktree registry): an earlier copy at the same path left them."""
    if not _IN_BOX:
        e2e_adoptions.forget_checkout_state(dst)
    for d in (dst, Path(f"{dst}-worktrees")):
        _emptied(d)
    shutil.copytree(EXAMPLE, dst, dirs_exist_ok=True)
    if extra_toml:
        with (dst / "foldyard.toml").open("a") as fh:
            fh.write(extra_toml)
    subprocess.run(["git", "init", "-q"], cwd=str(dst), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(dst), check=True)
    subprocess.run(
        ["git", "-c", "user.email=e2e@foldyard", "-c", "user.name=e2e", "commit", "-qm", "init"],
        cwd=str(dst),
        check=True,
    )
    return dst


def _engine() -> str:
    return "podman"


def _engine_env() -> dict:
    """podman reads CONTAINER_HOST; in the dev box only DOCKER_HOST is preset, so mirror it
    (otherwise raw `podman` falls back to its broken in-box local mode)."""
    env = dict(os.environ)
    if not env.get("CONTAINER_HOST") and env.get("DOCKER_HOST"):
        env["CONTAINER_HOST"] = env["DOCKER_HOST"]
    if _IN_BOX:
        env["IN_DEVBOX"] = "1"  # see _IN_BOX: the fixture scrubbed it from os.environ
    return env


def _engine_reachable() -> bool:
    eng = _engine()
    if not shutil.which(eng):
        return False
    try:
        return (
            subprocess.run(
                [eng, "info"], env=_engine_env(), capture_output=True, timeout=20
            ).returncode
            == 0
        )
    except Exception:
        return False


def _service_is_listed(output: str, project: str, service: str) -> bool:
    """Accept provider-specific columns and separators in compose ps output."""
    expected = f"{project}-{service}"
    rows = (line.split() for line in output.splitlines() if line.split())
    names = (value.replace("_", "-") for row in rows for value in (row[0], row[-1]))
    return any(name == expected or name.startswith(f"{expected}-") for name in names)


pytestmark = pytest.mark.skipif(
    os.environ.get("FOLDYARD_E2E") != "1" or not _engine_reachable(),
    reason="live e2e: set FOLDYARD_E2E=1 and have a reachable podman engine",
)


def _foldyard(
    args: list[str], repo: Path, timeout: int = 900, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Invoke the real CLI (`python -m foldyard …`) with the example as the repo root, on the
    actually-installed engine (overriding conftest's golden-test FOLDYARD_ENGINE pin).

    On the host the CLI gets no CONTAINER_HOST: that export (`e2e_host.export_vm_socket`, the
    host-e2e jobs) is for the TESTS' own engine calls, and an operator's shell has none — Lima
    registers no podman connection either. Inherited, it reached the supervisor `fy up` starts
    and hid a reconcile that derived no socket of its own; the CLI must find the
    VM's socket itself. In a box it is the box's own engine selection, kept."""
    return subprocess.run(
        [sys.executable, "-m", "foldyard", *args],
        cwd=str(repo),
        env=_cli_env(repo, env_extra),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _cli_env(repo: Path, env_extra: dict[str, str] | None = None) -> dict[str, str]:
    """The real CLI's environment (see :func:`_foldyard`)."""
    env = _engine_env()
    if not _IN_BOX:
        env.pop("CONTAINER_HOST", None)
    return {**env, "FOLDYARD_REPO": str(repo), "FOLDYARD_ENGINE": _engine(), **(env_extra or {})}


def _followed_logs(repo: Path) -> tuple[list[str], str, int]:
    """`fy logs` (which follows) until it has relayed a line, then Ctrl-C as the terminal sends it
    (to the whole foreground group): the lines it printed, its stderr and its exit code."""
    import signal
    import threading

    proc = subprocess.Popen(
        [sys.executable, "-m", "foldyard", "logs"],
        cwd=str(repo),
        env=_cli_env(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert proc.stdout is not None and proc.stderr is not None
    lines: list[str] = []
    arrived = threading.Event()

    def read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            arrived.set()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        # Through a pipe, podman-compose's print()s sit in a block buffer unless it is told not to.
        assert arrived.wait(120), "fy logs relayed nothing while it followed"
    finally:
        os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            raise
    reader.join(timeout=10)
    return lines, proc.stderr.read(), proc.returncode


def _adopt_on_host(repo: Path, env_extra: dict[str, str] | None = None) -> None:
    """What an operator does before the first `up` on a checkout: read the config, then
    `fy config adopt` it (ADR-0022). The launch gate REFUSES an unadopted checkout when there is
    no terminal to adopt on — which is every CI run — rather than adopting unattended. Inside a
    box there is nothing to adopt (the pin lives on the host and `in_box()` skips the gate), and
    `adopt` refuses there, so this is host-only."""
    if _IN_BOX:
        return
    # Recorded first, so an adoption a failed adopt still wrote is forgotten with the rest.
    e2e_adoptions.record(repo)  # forgotten when the module ends (conftest)
    adopted = _foldyard(["config", "adopt"], repo, timeout=60, env_extra=env_extra)
    assert adopted.returncode == 0, f"fy config adopt failed:\n{adopted.stdout}\n{adopted.stderr}"


@pytest.fixture
def example_repo(tmp_path):
    """A throwaway git copy of example/ (foldyard's main_repo() shells git), so up/down never
    touch the committed tree: the host tier's one checkout on your computer (:data:`HOST_TIER`).
    Tears the stack down even if the test body fails mid-way."""
    dst = fresh_example(tmp_path / "example" if _IN_BOX else host_checkout())
    _adopt_on_host(dst)
    yield dst
    _foldyard(["down"], dst, timeout=180)  # belt-and-suspenders if the body raised early


def _probe_db(network: str = NETWORK, timeout: int = 120, env_args: list[str] | None = None) -> str:
    """Hit the api's /db across the compose network via a throwaway container — works whether
    or not the test runner can reach the engine's published ports directly (the box can't).
    `env_args` are extra `--env` flags for the probe container (a walled VM propagates its proxy
    env into containers; the host-tier wall test clears it so wget talks to `api` directly)."""
    eng, env = _engine(), _engine_env()
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        out = subprocess.run(
            [eng, "run", "--rm", "--network", network, *(env_args or []),
             "docker.io/library/alpine", "wget", "-qO-", API_DB_URL],
            env=env, capture_output=True, text=True, timeout=40,
        )  # fmt: skip
        last = (out.stdout + out.stderr).strip()
        if "reachable" in last:
            return last
        time.sleep(3)
    return last


def test_example_stack_up_serves_then_down(example_repo):
    up = _foldyard(["up"], example_repo)
    assert up.returncode == 0, f"foldyard up failed:\n{up.stdout}\n{up.stderr}"

    ps = _foldyard(["ps"], example_repo, timeout=60)
    ps_output = ps.stdout + ps.stderr
    assert _service_is_listed(ps_output, PROJECT, "api"), ps_output

    body = _probe_db()
    assert "reachable" in body, f"api /db never became reachable; last response: {body!r}"

    # The config-hash drift check against labels a real `up` just wrote.
    state = _foldyard(["state"], example_repo, timeout=60)
    assert "match the rendered config" in state.stdout, state.stdout + state.stderr

    # What the stack prints is relayed a line at a time with its control sequences stripped (its
    # own colour too), and the terminal's Ctrl-C ends the follow without a traceback: not fy's, and
    # not the compose client's (it runs in a process group of its own, so the Ctrl-C reaches fy
    # alone, which stops relaying before it stops the client).
    lines, err, rc = _followed_logs(example_repo)
    assert not [line for line in lines if "\x1b" in line], lines
    # Nor the interpreter aborting at shutdown under a relay still printing.
    assert "Traceback" not in err and "Fatal Python error" not in err, err
    assert rc >= 0, f"ended by signal {-rc}: {err}"

    down = _foldyard(["down"], example_repo, timeout=180)
    assert down.returncode == 0, down.stderr
    remaining = subprocess.run(
        [
            _engine(), "ps", "-a", "--format", "{{.Names}}", "--filter",
            f"label=com.docker.compose.project={PROJECT}",
        ],
        env=_engine_env(), capture_output=True, text=True, timeout=30,
    )  # fmt: skip
    assert remaining.returncode == 0, remaining.stderr
    left = remaining.stdout.strip()
    assert left == "", f"containers left after down: {left!r}"


@pytest.fixture
def example_with_worktree(tmp_path):
    """A throwaway example repo plus a real sibling Git worktree, mirroring the host layout."""
    main = fresh_example(tmp_path / "example" if _IN_BOX else host_checkout())
    wt_root = Path(f"{main}-worktrees")
    feat = wt_root / "feat"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-b", "feat", str(feat)], check=True)
    env = {"FOLDYARD_WORKTREES_ROOT": str(wt_root)}
    _adopt_on_host(main, env)
    if not _IN_BOX:  # not os.environ: the autouse fixture scrubbed IN_DEVBOX (see _IN_BOX)
        # The host acts only on worktrees it recorded (worktree_registry). A worktree made with
        # plain `git worktree add` is registered the way an operator would: `fy worktree add` on
        # the existing checkout. Host-only — in the box there is no registry (and the verb refuses).
        reg = _foldyard(["worktree", "add", "feat", "--yes"], main, env_extra=env)
        assert reg.returncode == 0, f"registering feat failed:\n{reg.stdout}\n{reg.stderr}"
    _adopt_on_host(feat, {**env, "WORKTREE": "feat"})
    yield main, feat, env
    _foldyard(["down"], feat, timeout=180, env_extra={**env, "WORKTREE": "feat"})
    _foldyard(["down"], main, timeout=180, env_extra=env)


def test_example_main_and_worktree_stacks_are_namespaced(example_with_worktree):
    main, feat, env = example_with_worktree
    feat_env = {**env, "WORKTREE": "feat"}

    main_up = _foldyard(["up"], main, env_extra=env)
    assert main_up.returncode == 0, f"main foldyard up failed:\n{main_up.stdout}\n{main_up.stderr}"
    feat_up = _foldyard(["up"], feat, env_extra=feat_env)
    assert feat_up.returncode == 0, f"feat foldyard up failed:\n{feat_up.stdout}\n{feat_up.stderr}"

    main_ps = _foldyard(["ps"], main, timeout=60, env_extra=env)
    feat_ps = _foldyard(["ps"], feat, timeout=60, env_extra=feat_env)
    main_output = main_ps.stdout + main_ps.stderr
    feat_output = feat_ps.stdout + feat_ps.stderr
    assert _service_is_listed(main_output, PROJECT, "api"), main_output
    assert _service_is_listed(feat_output, f"{PROJECT}-feat", "api"), feat_output

    assert "reachable" in _probe_db("fyex_default")
    assert "reachable" in _probe_db("fyex-feat_default")
