"""Opt-in LIVE end-to-end test: drive the real foldyard CLI against the example consumer on
a real container engine, and assert the stack actually serves a DB-backed request.

Unlike the golden/mocked tests, this runs the engine for real — `foldyard up` builds the api
image, pulls postgres, and the test queries the DB across the compose network. It is OPT-IN:
skipped unless FOLDYARD_E2E=1 AND an engine is reachable (it pulls images + builds, ~minutes).
Run it where foldyard has an engine — the dev box (the rootless machine socket), a nested
podman host (see DEVELOPMENT.md), or Linux CI with `MACHINE_BACKEND=native`. It namespaces under
the example's own 'fyex' project, so it never collides with a host project's stack.

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

EXAMPLE = Path(__file__).resolve().parent.parent / "example"

# The autouse `scrubbed_box_session_env` fixture (conftest) strips IN_DEVBOX from every test's
# environment so the golden tests behave identically on a host, in CI and in a box. This test
# drives the REAL CLI as a subprocess, and that CLI must know where it is: in a box — the dev box,
# or CI's docker-CLI container — the launch gates stand down (no VM to manage, nothing host-side
# to adopt), and without the signature preflight refuses `up` over the default backend's missing
# limactl. Read it at import, before the fixture runs, and hand it back to the subprocess.
_IN_BOX = os.environ.get("IN_DEVBOX") == "1"
PROJECT = "fyex"  # the example's [project].prefix
NETWORK = f"{PROJECT}_default"  # compose's default network for that project
API_DB_URL = "http://api:8080/db"  # the api service (in-network) + the DB-wiring endpoint


def _engine() -> str:
    """The actually-installed engine. NB: ignore FOLDYARD_ENGINE here — conftest pins it to
    'podman' for golden-test determinism, but a LIVE test must use whatever is really on PATH
    (e.g. the dev box has only the `docker` CLI, which speaks the podman socket)."""
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
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
    reason="live e2e: set FOLDYARD_E2E=1 and have a reachable podman/docker engine",
)


def _foldyard(
    args: list[str], repo: Path, timeout: int = 900, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Invoke the real CLI (`python -m foldyard …`) with the example as the repo root, on the
    actually-installed engine (overriding conftest's golden-test FOLDYARD_ENGINE pin)."""
    return subprocess.run(
        [sys.executable, "-m", "foldyard", *args],
        cwd=str(repo),
        env={
            **_engine_env(),
            "FOLDYARD_REPO": str(repo),
            "FOLDYARD_ENGINE": _engine(),
            **(env_extra or {}),
        },
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _adopt_on_host(repo: Path, env_extra: dict[str, str] | None = None) -> None:
    """What an operator does before the first `up` on a checkout: read the config, then
    `fy config adopt` it (ADR-0022). The launch gate REFUSES an unadopted checkout when there is
    no terminal to adopt on — which is every CI run — rather than adopting unattended. Inside a
    box there is nothing to adopt (the pin lives on the host and `in_box()` skips the gate), and
    `adopt` refuses there, so this is host-only."""
    if _IN_BOX:
        return
    adopted = _foldyard(["config", "adopt"], repo, timeout=60, env_extra=env_extra)
    assert adopted.returncode == 0, f"fy config adopt failed:\n{adopted.stdout}\n{adopted.stderr}"


@pytest.fixture
def example_repo(tmp_path):
    """A throwaway git copy of example/ (foldyard's main_repo() shells git), so up/down never
    touch the committed tree. Tears the stack down even if the test body fails mid-way."""
    dst = tmp_path / "example"
    shutil.copytree(EXAMPLE, dst)
    subprocess.run(["git", "init", "-q"], cwd=str(dst), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(dst), check=True)
    subprocess.run(
        ["git", "-c", "user.email=e2e@foldyard", "-c", "user.name=e2e", "commit", "-qm", "init"],
        cwd=str(dst),
        check=True,
    )
    _adopt_on_host(dst)
    yield dst
    _foldyard(["down"], dst, timeout=180)  # belt-and-suspenders if the body raised early


def _probe_db(network: str = NETWORK, timeout: int = 120) -> str:
    """Hit the api's /db across the compose network via a throwaway container — works whether
    or not the test runner can reach the engine's published ports directly (the box can't)."""
    eng, env = _engine(), _engine_env()
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        out = subprocess.run(
            [eng, "run", "--rm", "--network", network,
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
    main = tmp_path / "example"
    shutil.copytree(EXAMPLE, main)
    subprocess.run(["git", "init", "-q"], cwd=str(main), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(main), check=True)
    subprocess.run(
        ["git", "-c", "user.email=e2e@foldyard", "-c", "user.name=e2e", "commit", "-qm", "init"],
        cwd=str(main),
        check=True,
    )
    wt_root = tmp_path / "example-worktrees"
    feat = wt_root / "feat"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-b", "feat", str(feat)], check=True)
    env = {"FOLDYARD_WORKTREES_ROOT": str(wt_root)}
    _adopt_on_host(main, env)
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
