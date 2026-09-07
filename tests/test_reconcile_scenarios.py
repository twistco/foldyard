"""Reconciler conformance scenarios — the de-risk harness for consolidating the reconcilers.

Unlike the per-module unit/golden tests (which pin each reconciler's INTERNALS), these pin
the ORCHESTRATION: which reconciler fires on which trigger, with what inputs, across
posture/lifecycle scenarios — driven through REAL devmode state I/O (isolated to tmp), with
recorders only at the seams that must survive any refactor (`stack.reconcile_posture`, the
engine, `Child`). The reconciler-consolidation work (mode-state consolidation proposal A)
must keep every scenario here green byte-for-byte; a refactor that changes what fires when
is a behaviour change, not a refactor.

Scenario map (trigger → expected reconcilers):
  mode change            → stack posture reconcile (prev/new signatures), mirror refresh
                           (only if a box is up / mirror exists)
  supervisor tick        → TTL expiry+settle, daemon convergence, mirror write (up boxes
                           only), capability probe publish
  TTL lapse on a tick    → durable settled write + the daemon for the lapsed rung dropped
"""

from __future__ import annotations

import json
import types
from datetime import timedelta

import pytest

from foldyard import devmode, stack, supervisor

pytestmark = pytest.mark.usefixtures("full_config_bound")


@pytest.fixture
def recorded_stack_reconcile(monkeypatch):
    """Record every stack-posture reconcile invocation (the mode-change seam)."""
    calls: list[dict] = []

    def _record(prev, new, cfg=None, sink=None):
        calls.append({"prev": prev, "new": new})
        return True

    monkeypatch.setattr(stack, "reconcile_posture", _record)
    return calls


@pytest.fixture
def tick_world(isolated_state, monkeypatch):
    """A supervisor-tick world on REAL devmode state: fake children, no real engine,
    controllable box-up set. Returns knobs + recorders."""
    launched: list[str] = []

    class FakeChild:
        def __init__(self, name, spec):
            self.name, self.spec = name, spec
            self.signature = repr((spec["cmd"], sorted(spec["env"].items())))
            self.started_at = 0.0
            self.proc = types.SimpleNamespace(returncode=None)
            launched.append(name)

        def alive(self):
            return True

        def stop(self):
            launched.append(f"stop:{self.name}")

    world = {"up": [], "launched": launched, "state": isolated_state}
    monkeypatch.setattr(supervisor, "Child", FakeChild)
    monkeypatch.setattr(supervisor.allowlist, "sweep", lambda: None)
    monkeypatch.setattr(supervisor.githeal, "sweep", lambda log: None)
    monkeypatch.setattr(supervisor.devmode, "up_worktrees", lambda: world["up"])
    monkeypatch.setattr(supervisor.devmode, "probe", lambda port, host=None: False)
    monkeypatch.setattr(devmode, "probe", lambda port, host=None: False)
    return world


# ── trigger: mode change ─────────────────────────────────────────────────────────────────


def test_mode_change_reconciles_stack_with_the_posture_signatures(
    isolated_state, recorded_stack_reconcile
):
    devmode.set_mode({"gcp": "sa"})
    (call,) = recorded_stack_reconcile
    assert call["prev"]["env"].get("COMPOSE_PROFILES", "") == ""
    assert call["new"]["env"]["COMPOSE_PROFILES"] == "metadata"
    # the identity overlays ride the same signature (overlay/env-only changes must reconcile)
    assert any("identity" in o for o in call["new"]["overlays"])


def test_env_only_mode_change_rides_the_same_stack_seam(isolated_state, recorded_stack_reconcile):
    # github=app flips no compose profile or overlay — only the derived env (GH_INJECT). That
    # env-only diff must still flow through the ONE stack seam as a signature change (the
    # posture-signature fix: comparing profiles alone missed exactly this class).
    devmode.set_mode({"github": "app"})
    (call,) = recorded_stack_reconcile
    assert call["prev"]["overlays"] == call["new"]["overlays"]
    assert call["prev"]["env"].get("COMPOSE_PROFILES") == call["new"]["env"].get("COMPOSE_PROFILES")
    assert call["new"]["env"]["GH_INJECT"] == "app" and "GH_INJECT" not in call["prev"]["env"]


def test_back_to_back_mode_changes_chain_their_posture_signatures(
    isolated_state, recorded_stack_reconcile
):
    # Rapid toggling (two set_mode calls with nothing between them — the TUI's quick-toggle
    # path) must produce CHAINED reconcile inputs: the second call's prev signature is exactly
    # the first call's new one, so replaying the reconciles in order converges the stack on the
    # final posture with no lost update — and a toggle-back lands on the original signature.
    devmode.set_mode({"gcp": "sa"})
    devmode.set_mode({"gcp": "off"})
    first, second = recorded_stack_reconcile
    assert second["prev"] == first["new"]  # no gap for a concurrent writer to hide in
    assert second["new"] == first["prev"]  # a round-trip restores the starting signature
    assert second["new"]["env"].get("COMPOSE_PROFILES", "") == ""


def test_mode_change_refreshes_mirror_only_when_a_box_is_up(
    isolated_state, recorded_stack_reconcile, monkeypatch
):
    monkeypatch.setattr(devmode, "up_worktrees", lambda: [])
    devmode.set_mode({"gcp": "logs"})
    assert not isolated_state["mirror"].exists()  # down box: never dirty the checkout
    monkeypatch.setattr(devmode, "up_worktrees", lambda: [""])
    devmode.set_mode({"gcp": "sa"})
    assert json.loads(isolated_state["mirror"].read_text())["gcp"] == "sa"


# ── trigger: supervisor tick ─────────────────────────────────────────────────────────────


def test_tick_converges_daemons_and_writes_mirror_and_capabilities_for_up_box(
    tick_world, recorded_stack_reconcile, monkeypatch
):
    probe_calls: list[str] = []

    def _fake_probe():
        probe_calls.append("ran")
        return True, "chain ok"

    from foldyard.plugins import CapabilityProbe

    monkeypatch.setattr(
        supervisor.devmode,
        "capability_probes",
        lambda mode: (
            [CapabilityProbe(axis="gcp", name="t", check=_fake_probe, interval=3600.0)]
            if mode.get("gcp", "off") != "off"
            else []
        ),
    )
    devmode.set_mode({"gcp": "sa"})
    tick_world["up"] = [""]

    supervisor.reconcile_once({}, {})

    # daemons: the posture's minter AND the always-on proxy converged
    assert {"gcp-minter", "egress-proxy"} <= set(tick_world["launched"])
    # mirror: written for the up box, carrying mode + daemon status + capabilities
    mirror = json.loads(tick_world["state"]["mirror"].read_text())
    assert mirror["gcp"] == "sa" and "daemons" in mirror
    assert mirror["capabilities"]["gcp"]["ok"] is True
    # capabilities also published host-side
    caps = json.loads((tick_world["state"]["dir"] / "capabilities.json").read_text())
    assert caps[""]["gcp"]["detail"] == "chain ok"
    assert probe_calls == ["ran"]


def test_tick_with_nothing_up_serves_main_daemons_but_writes_no_mirror(tick_world):
    devmode.set_mode({"gcp": "sa"})
    tick_world["state"]["mirror"].unlink(missing_ok=True)  # set_mode may have refreshed it
    tick_world["up"] = []

    supervisor.reconcile_once({}, {})

    assert "gcp-minter" in tick_world["launched"]  # main fallback keeps the daemons alive
    assert not tick_world["state"]["mirror"].exists()  # …but no checkout is dirtied


def test_tick_expires_settles_and_drops_the_lapsed_rungs_daemon(tick_world):
    devmode.set_mode({"gcp": "sa", "llm": "live"})
    devmode.set_mode({"gcp": "user"}, ttl=60)
    raw = json.loads(tick_world["state"]["auth"].read_text())
    raw["expires"]["gcp"] = devmode._iso(devmode.now() - timedelta(hours=1))
    tick_world["state"]["auth"].write_text(json.dumps(raw))
    tick_world["up"] = [""]

    children: dict = {}
    supervisor.reconcile_once(children, {})

    durable = devmode.read(apply_expiry=False)["mode"]
    assert durable["gcp"] == "off" and durable["llm"] == "off"  # expiry + settle, durably
    assert "gcp-minter" not in children  # the lapsed rung's daemon is not desired
    mirror = json.loads(tick_world["state"]["mirror"].read_text())
    assert mirror["gcp"] == "off" and mirror["llm"] == "off"


def test_tick_stops_a_daemon_the_new_posture_no_longer_wants(tick_world):
    devmode.set_mode({"gcp": "sa"})
    tick_world["up"] = [""]
    children: dict = {}
    supervisor.reconcile_once(children, {})
    assert "gcp-minter" in children

    devmode.set_mode({"gcp": "off"})
    supervisor.reconcile_once(children, {})
    assert "gcp-minter" not in children
    assert "stop:gcp-minter" in tick_world["launched"]
