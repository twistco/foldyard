"""`fy host` — the ONE host-side supervisor — with the zero-secret rig (docs/testing-modes.md).

The example declares `[plugins.fakecred]`: `fakecred` is a credential-granting axis whose `on`
runs a real daemon (the fake minter) with a capability probe, `fakedep` a dependent axis that
CONSUMES it and layers `compose.feature.yml` onto the stack. So `fy mode fakecred=on fakedep=on`
on the host exercises, with nothing secret anywhere: the supervisor's heartbeat, the daemon
reconcile (the minter comes up, `blocked-daemons.json` stays empty — there is no host.env secret
to be missing), the stack tier's overlay reconcile (the running stack is re-rendered with the
overlay, and the api reports `feature: on`), and the way back down.

Host tier (tests/e2e_host.py). `fy mode <set>` is a host-only verb — refused in a box — which
is the reason this could never run in the container mirror.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from e2e_host import (
    NETWORK,
    _adopt_on_host,
    _engine,
    _engine_env,
    ensure_vm,
    example_copy,
    fy,
    fy_ok,
    heartbeat_age,
    host_tier,
    state_dir,
    wait_for,
)

pytestmark = host_tier


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = example_copy(tmp_path_factory.mktemp("daemons"))
    _adopt_on_host(r)
    ensure_vm(r)
    fy_ok(["up"], r)
    yield r
    fy(["mode", "fakecred=off", "fakedep=off"], r, timeout=120)
    fy(["down"], r, timeout=180)


def _api_root() -> str:
    """GET the api's `/` across the compose network (the feature flag lives there)."""
    out = subprocess.run(
        [
            _engine(), "run", "--rm", "--network", NETWORK,
            "docker.io/library/alpine", "wget", "-qO-", "http://api:8080/",
        ],
        env=_engine_env(), capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    return (out.stdout + out.stderr).strip()


def test_the_supervisor_is_up_and_heartbeating_after_up(repo):
    age = wait_for(
        lambda: (a := heartbeat_age()) is not None and a < 15 and a,
        timeout=60,
        what="a fresh supervisor heartbeat",
    )
    assert age < 15


def test_mode_on_brings_the_fake_minter_up_and_re_renders_the_stack_with_the_overlay(repo):
    fy_ok(["mode", "fakecred=on", "fakedep=on"], repo, timeout=120)
    # The supervisor reconciles on its tick: daemons AND the stack tier (the overlay). `fy state`
    # exits non-zero while anything drifts, so a clean exit is the whole reconcile having landed.
    clean = wait_for(
        lambda: (r := fy(["state"], repo, timeout=60)).rc == 0 and r,
        timeout=180,
        what="fy state clean",
    )
    assert "fake-minter" in clean.out, clean.out
    assert "compose.feature.yml" in clean.out, clean.out
    blocked = state_dir() / "blocked-daemons.json"
    if blocked.exists():
        assert json.loads(blocked.read_text() or "{}") == {}, blocked.read_text()
    body = wait_for(
        lambda: (b := _api_root()) and '"feature"' in b and b, timeout=120, what="the api's /"
    )
    assert '"on"' in body, body


def test_mode_off_takes_the_fake_minter_down_again(repo):
    fy_ok(["mode", "fakecred=off", "fakedep=off"], repo, timeout=120)
    clean = wait_for(
        lambda: (r := fy(["state"], repo, timeout=60)).rc == 0 and r,
        timeout=180,
        what="fy state clean",
    )
    assert "fake-minter" not in clean.out, clean.out
    assert "compose.feature.yml" not in clean.out, clean.out
