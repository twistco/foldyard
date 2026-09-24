"""`fy state` — the desired-vs-observed view over every state tier (consolidation proposal A,
observability half). Engine-touching scopes are stubbed; the point here is that each tier's
agreement/disagreement renders as the right status with the fix in the row."""

from __future__ import annotations

import json

import pytest

from foldyard import devmode, state_view

pytestmark = pytest.mark.usefixtures("full_config_bound")


@pytest.fixture
def offline_engine(monkeypatch):
    """Make the engine-backed scopes deterministic: stack/box report 'unknown/down'."""

    monkeypatch.setattr(devmode, "ps_labels", lambda *a, **k: None)  # engine unreachable
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

    running = [
        (
            "acme_api_1",
            {
                "com.docker.compose.project.config_files": (
                    "/repo/compose.yml,/repo/dev-stack/compose.identity.yml,"
                    "/repo/dev-stack/compose.identity-data.yml,"
                    "/repo/dev-stack/compose.storage-staging.yml"
                )
            },
        )
    ]
    monkeypatch.setattr(devmode, "ps_labels", lambda *a, **k: running)
    rc = state_view.show()
    out = capsys.readouterr().out
    assert rc == 1 and "stale overlay" in out and "compose.storage-staging.yml" in out


@pytest.fixture
def overlay_repo(tmp_path, isolated_state):
    """The full config bound to a checkout whose posture overlays EXIST, each touching only the
    services a real consumer's would: the identity overlays re-render the workers, never
    postgres — so which containers an overlay leaves alone is observable (#33)."""
    from conftest import FULL_TOML
    from foldyard import config

    stack_dir = tmp_path / "repo" / "dev-stack"
    stack_dir.mkdir(parents=True)
    (stack_dir / "compose.identity.yml").write_text(
        "services:\n  queue-worker:\n    environment: {A: '1'}\n  app:\n    environment: {}\n"
    )
    (stack_dir / "compose.identity-data.yml").write_text(
        "services:\n  queue-worker:\n    environment: {B: '1'}\n"
    )
    (stack_dir / "compose.storage-staging.yml").write_text(
        "services:\n  app:\n    environment: {C: '1'}\n"
    )
    with config.using(config.Config(repo_root=tmp_path / "repo", worktree="", toml=FULL_TOML)):
        yield stack_dir


def _container(service: str, *overlays: str) -> tuple[str, dict]:
    files = ",".join(["/repo/compose.yml", *(f"/repo/dev-stack/{o}" for o in overlays)])
    return (
        f"tangible_{service}_1",
        {
            "com.docker.compose.service": service,
            "com.docker.compose.project.config_files": files,
        },
    )


def _stack_rows(monkeypatch, running):
    from foldyard import reconcile

    monkeypatch.setattr(devmode, "ps_labels", lambda *a, **k: running)
    return reconcile.StackScope().rows(devmode.read())


def test_stack_drift_is_judged_per_container_not_by_the_longest_label(overlay_repo, monkeypatch):
    # #33: podman-compose recreates only the services an overlay changes, so postgres keeps a
    # label from an earlier posture indefinitely. It carrying the full -f list must NOT vouch for
    # a queue-worker that was never re-rendered onto the identity overlays.
    devmode.set_mode({"gcp": "sa"})
    (row,) = _stack_rows(
        monkeypatch,
        [
            _container("postgres", "compose.identity.yml", "compose.identity-data.yml"),
            _container("queue-worker"),
        ],
    )
    assert row.status == "drift"
    assert "queue-worker" in row.observed and "postgres" not in row.observed
    assert "compose.identity.yml" in row.observed


def test_stack_label_predating_an_overlay_is_not_drift_when_it_leaves_that_service_alone(
    overlay_repo, monkeypatch
):
    # The converse: compose never recreated postgres for the identity overlays because they don't
    # define it — its older label is correct, not drift.
    devmode.set_mode({"gcp": "sa"})
    (row,) = _stack_rows(
        monkeypatch,
        [
            _container("postgres"),
            _container("queue-worker", "compose.identity.yml", "compose.identity-data.yml"),
            _container("app", "compose.identity.yml", "compose.identity-data.yml"),
        ],
    )
    assert row.status == "ok", row.observed


def test_stack_stale_overlay_only_counts_for_the_services_it_touched(overlay_repo, monkeypatch):
    # storage-staging only ever touched `app`: postgres still labelled with it is fine, app still
    # carrying it is drift.
    devmode.set_mode({"gcp": "sa"})
    identity = ("compose.identity.yml", "compose.identity-data.yml")
    (ok,) = _stack_rows(
        monkeypatch,
        [
            _container("postgres", "compose.storage-staging.yml"),
            _container("queue-worker", *identity),
            _container("app", *identity),
        ],
    )
    assert ok.status == "ok", ok.observed
    (row,) = _stack_rows(
        monkeypatch,
        [
            _container("postgres", "compose.storage-staging.yml"),
            _container("app", *identity, "compose.storage-staging.yml"),
        ],
    )
    assert row.status == "drift"
    assert "app" in row.observed and "stale overlay" in row.observed
    assert "postgres" not in row.observed


def test_stack_unreadable_overlay_counts_as_touching_every_service(overlay_repo, monkeypatch):
    # When the overlay can't be read, "does it touch this service?" has no answer — fall back to
    # the conservative reading (drift), never to a silent ✓.
    devmode.set_mode({"gcp": "sa"})
    (overlay_repo / "compose.identity-data.yml").write_text("services: [not, a, mapping")
    (row,) = _stack_rows(monkeypatch, [_container("postgres", "compose.identity.yml")])
    assert row.status == "drift" and "compose.identity-data.yml" in row.observed


def test_state_names_a_blocked_daemons_reason(isolated_state, offline_engine, monkeypatch, capsys):
    # The spawn gate's reason, published by the supervisor, replaces the generic DOWN advice.
    devmode.set_mode({"gcp": "sa"})
    monkeypatch.setattr(
        devmode,
        "daemon_status",
        lambda mode: {
            "gcp-minter": {"up": True, "port": 41100, "blocked": "can't bind :41100 — foreign"}
        },
    )
    rc = state_view.show()
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED — can't bind :41100 — foreign" in out and "DOWN" not in out
