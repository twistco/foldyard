"""The read-only engine probes, LIVE: the functions whose output is exactly what drifts between
podman versions, and which the hermetic unit suite (PR #16) can no longer reach.

Run in-process against the example stack, up once per module on the real VM: `devmode.workspaces`
/ `up_worktrees` / `_stack_mounts` / `_stack_shadow_check`, `stack.disk_headroom`,
`machine.state/socket/responsive` (incl. the paired invariant: the flag survives a dead socket),
`reconcile.scopes()` rows, and the CLI surfaces over them (`fy state`, `fy doctor`). Field NAMES are
asserted deliberately — `graphRootAllocated/Used`, `.Label`, compose labels — because a renamed
field is the drift to catch: the probe then reads "unreachable" or "none" while the stack runs.

Host tier (tests/e2e_host.py). Written against Lima 2.2.0, a Fedora 44 guest (podman 5.8.4), host
podman CLI 4.9.3 — the host CLI speaks to the guest's API, so BOTH versions are in play here.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from e2e_host import (
    LIMA_SOCK,
    PROJECT,
    VM,
    _adopt_on_host,
    _engine,
    _engine_env,
    ensure_vm,
    example_copy,
    fy,
    fy_ok,
    host_tier,
)

pytestmark = host_tier


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = example_copy(tmp_path_factory.mktemp("probes"))
    _adopt_on_host(r)
    ensure_vm(r)
    fy_ok(["up"], r)
    yield r
    fy(["down"], r, timeout=180)


@pytest.fixture
def bound(repo, monkeypatch):
    """Bind the in-process package to the example copy: env + cwd, caches cleared (config's
    lru_caches, the plugin registry) and the machine module's import-time constants re-pointed —
    they bind from config at import, which may have happened for another checkout earlier in
    the session (DEVELOPMENT.md, conventions)."""
    from foldyard import config, machine, machine_backend, plugins

    monkeypatch.setenv("FOLDYARD_REPO", str(repo))
    monkeypatch.setenv("FOLDYARD_ENGINE", _engine())
    monkeypatch.chdir(repo)
    config.clear_caches()
    plugins._REGISTRY_CACHE.clear()
    monkeypatch.setattr(machine, "MACHINE", VM)
    monkeypatch.setattr(machine, "BACKEND", machine_backend.get_backend("lima"))
    yield
    config.clear_caches()
    plugins._REGISTRY_CACHE.clear()


def test_workspaces_sees_main_with_its_live_stack(bound):
    from foldyard import devmode

    rows = devmode.workspaces()
    assert [r["name"] for r in rows] == ["main"], rows
    main = rows[0]
    assert main["project"] == PROJECT
    assert main["containers"] == 2, (
        f"expected the db + api containers, got {main}"
    )  # the example stack
    assert main["devbox"] is False
    assert main["branch"]
    # The example declares no [project].app_port, so no browsable URL: None is the contract
    # (a consumer-defined key, never a guessed APP_PORT — config.app_port_key).
    assert main["app_port"] is None


def test_up_worktrees_counts_dev_boxes_not_stack_containers(bound):
    from foldyard import devmode

    # The stack is up, no dev box is: a stack container must never read as an up box.
    assert devmode.up_worktrees() == []


def test_stack_mounts_reads_the_running_containers(bound):
    from foldyard import devmode

    containers = devmode._stack_mounts(PROJECT)
    # Rows are keyed by CONTAINER name (podman-compose: `<project>_<service>_1`).
    by_name = {name.replace("-", "_"): (binds, masked) for name, binds, masked in containers}
    assert set(by_name) == {f"{PROJECT}_db_1", f"{PROJECT}_api_1"}, containers
    api_binds, _ = by_name[f"{PROJECT}_api_1"]
    assert api_binds == [], f"the example api mounts nothing from the checkout: {api_binds}"
    _, db_masked = by_name[f"{PROJECT}_db_1"]
    assert "/var/lib/postgresql/data" in db_masked, f"postgres's VOLUME not in masked: {db_masked}"


def test_stack_shadow_check_yields_placeholder_then_rows(bound):
    from foldyard import devmode

    rows = list(devmode._stack_shadow_check())
    assert rows, "a running stack must yield at least the placeholder"
    assert rows[0][0] == "running" and rows[0][1] == "stack shadowing", rows
    # Doctor's contract: every placeholder is resolved by a later real row of the same name.
    assert any(r[1] == "stack shadowing" and r[0] != "running" for r in rows[1:]), rows


def test_disk_headroom_reads_the_vm_store(bound):
    from foldyard import stack

    head = stack.disk_headroom(_engine_env())
    assert head is not None, "disk_headroom returned None — `podman system df` names changed?"
    assert head.total > head.used > 0, head
    assert head.render()


def test_machine_flag_socket_and_liveness_agree_then_diverge(bound):
    from foldyard import machine

    assert machine.exists()
    assert machine.state() == "running"
    assert machine.socket() == f"unix://{LIMA_SOCK}"
    assert machine.responsive()
    # The paired invariant (DEVELOPMENT.md): `state()` is a lifecycle FLAG, `responsive()` is a
    # real connect. Moving the socket aside kills the second and leaves the first untouched.
    aside = LIMA_SOCK.with_name("podman.sock.aside")
    os.rename(LIMA_SOCK, aside)
    try:
        assert machine.state() == "running"
        assert not machine.responsive()
    finally:
        os.rename(aside, LIMA_SOCK)
    assert machine.responsive()


def test_reconcile_scopes_see_the_live_stack(bound):
    from foldyard import devmode, reconcile

    state = devmode.read()
    rows = {}
    for scope in reconcile.scopes():
        for row in scope.rows(state):
            rows.setdefault(scope.name, []).append(row)
    assert set(rows) >= {"posture", "daemons", "stack", "box"}, rows
    stack_rows = rows["stack"]
    if any("engine unreachable" in (r.observed or "") for r in stack_rows):
        # Re-run the scope's exact engine call so the CI log shows WHY (a template the host CLI
        # rejects, a socket the env doesn't carry, a timeout…) instead of the scope's summary.
        probe = subprocess.run(
            [
                _engine(),
                "ps",
                "--filter",
                f"label=com.docker.compose.project={PROJECT}",
                "--format",
                '{{.Label "com.docker.compose.project.config_files"}}',
            ],
            capture_output=True,
            text=True,
            timeout=20,
            env=devmode._engine_env(),
        )
        raise AssertionError(
            f"stack scope says 'engine unreachable' with the stack up; the probe itself: "
            f"rc={probe.returncode}\nstdout={probe.stdout!r}\nstderr={probe.stderr!r}\n"
            f"env DOCKER_HOST={devmode._engine_env().get('DOCKER_HOST')!r}"
        )
    assert all(r.status != "unknown" for r in stack_rows), stack_rows


def test_fy_state_reports_no_drift_with_the_stack_up(repo):
    run = fy_ok(["state"], repo, timeout=60)
    assert "engine unreachable" not in run.out, run.out
    assert "✗" not in run.out, run.out


def test_fy_doctor_has_no_failing_row(repo):
    run = fy(["doctor"], repo, timeout=120)
    assert run.rc == 0, run.out  # doctor_cli exits 1 on any `fail` row
    assert "✗" not in run.out, run.out
