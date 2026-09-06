"""`fy state` — the desired-vs-observed view over every state tier (consolidation proposal A,
observability half). Engine-touching scopes are stubbed; the point here is that each tier's
agreement/disagreement renders as the right status with the fix in the row."""

from __future__ import annotations

import json

import pytest

from foldyard import devmode, reconcile, state_view

pytestmark = pytest.mark.usefixtures("full_config_bound")


@pytest.fixture
def offline_engine(monkeypatch):
    """Make the engine-backed scopes deterministic: stack/box report 'unknown/down'."""

    def _no_engine(*a, **k):
        raise OSError("no engine in tests")

    monkeypatch.setattr(reconcile.subprocess, "run", _no_engine)
    monkeypatch.setattr(devmode, "_box_env_hint", lambda mode: None)


def test_state_all_ok_when_fully_offline(isolated_state, offline_engine, capsys):
    rc = state_view.show()
    out = capsys.readouterr().out
    assert rc == 0
    assert "fully offline" in out and "✗" not in out


def test_state_flags_a_down_daemon_and_a_degraded_capability(
    isolated_state, offline_engine, monkeypatch, capsys
):
    devmode.set_mode({"gcp": "sa"})
    caps = isolated_state["dir"] / "capabilities.json"
    monkeypatch.setenv("FOLDYARD_CAPABILITIES_FILE", str(caps))
    caps.write_text(
        json.dumps({"": {"gcp": {"ok": False, "detail": "PAM lapsed", "checked": "t"}}})
    )
    monkeypatch.setattr(
        devmode, "daemon_status", lambda mode: {"gcp-minter": {"up": False, "port": 41100}}
    )
    rc = state_view.show()
    out = capsys.readouterr().out
    assert rc == 1  # drift somewhere → non-zero, scriptable
    assert "DOWN" in out and "DEGRADED — PAM lapsed" in out


def test_state_reports_standing_incoherence_as_posture_drift(
    isolated_state, offline_engine, capsys
):
    # A force-written incoherent posture (the pre-settle strand) must show at the posture tier.
    devmode.set_mode({"gcp": "sa", "llm": "record"})
    devmode.set_mode({"gcp": "off"}, force=True)
    rc = state_view.show()
    out = capsys.readouterr().out
    assert rc == 1 and "incoherent" in out


def test_state_flags_a_stale_extra_overlay_as_drift(isolated_state, monkeypatch, capsys):
    # A stack still carrying a posture overlay the CURRENT mode no longer wants (compose.llm.yml
    # after llm=off) is drift in the other direction — it must not read as healthy.
    devmode.set_mode({"gcp": "sa"})  # desires the identity overlays, NOT the storage one
    monkeypatch.setattr(devmode, "_box_env_hint", lambda mode: None)
    monkeypatch.setattr(
        devmode, "daemon_status", lambda mode: {"gcp-minter": {"up": True, "port": 41100}}
    )

    class _Out:
        returncode = 0
        stdout = (
            "/repo/compose.yml,/repo/dev-stack/compose.identity.yml,"
            "/repo/dev-stack/compose.identity-data.yml,/repo/dev-stack/compose.storage-staging.yml\n"
        )

    monkeypatch.setattr(reconcile.subprocess, "run", lambda *a, **k: _Out())
    rc = state_view.show()
    out = capsys.readouterr().out
    assert rc == 1 and "stale overlay" in out and "compose.storage-staging.yml" in out
