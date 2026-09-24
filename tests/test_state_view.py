"""`fy state` — the desired-vs-observed view over every state tier (consolidation proposal A,
observability half). Engine-touching scopes are stubbed; the point here is that each tier's
agreement/disagreement renders as the right status with the fix in the row."""

from __future__ import annotations

import json

import pytest

from foldyard import devmode, state_view

pytestmark = pytest.mark.usefixtures("full_config_bound")

HASH = "io.podman.compose.config-hash"  # conftest pins FOLDYARD_ENGINE=podman


@pytest.fixture(autouse=True)
def no_render(monkeypatch):
    """No unit test renders the ambient repo's compose files: the fresh config hash is
    'unavailable' unless a test says what it is (tests/test_confighash.py covers the render)."""
    from foldyard import reconcile

    monkeypatch.setattr(
        reconcile.StackScope, "_desired_hashes", lambda self: (None, "not rendered in unit tests")
    )


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


def _container(service: str, *overlays: str, hash: str = "current") -> tuple[str, dict]:
    files = ",".join(["/repo/compose.yml", *(f"/repo/dev-stack/{o}" for o in overlays)])
    return (
        f"tangible_{service}_1",
        {
            "com.docker.compose.service": service,
            "com.docker.compose.project.config_files": files,
            HASH: f"{service}-{hash}",
        },
    )


def _stack_rows(monkeypatch, running, rendered: set[str] | None = None):
    """The stack scope's rows over ``running``, with every ``rendered`` service's fresh hash
    ``<service>-current`` (default: every running service); None = the render is unavailable."""
    from foldyard import reconcile

    monkeypatch.setattr(devmode, "ps_labels", lambda *a, **k: running)
    if rendered is not None or running:
        names = (
            rendered
            if rendered is not None
            else {r[1]["com.docker.compose.service"] for r in running}
        )
        monkeypatch.setattr(
            reconcile.StackScope,
            "_desired_hashes",
            lambda self: ({n: f"{n}-current" for n in names}, ""),
        )
    return reconcile.StackScope().rows(devmode.read())


def _unrendered(monkeypatch):
    from foldyard import reconcile

    monkeypatch.setattr(
        reconcile.StackScope, "_desired_hashes", lambda self: (None, "podman-compose moved")
    )


def test_stack_drift_is_the_config_hash_not_the_longest_label(overlay_repo, monkeypatch):
    # #33: postgres keeps an older posture's config_files label for as long as nothing touches
    # it, so no label vouches for another container. What compose itself compares — the
    # service's config hash against a fresh render — says queue-worker is stale; the overlay
    # comparison then says why.
    devmode.set_mode({"gcp": "sa"})
    (row,) = _stack_rows(
        monkeypatch,
        [
            _container("postgres", "compose.identity.yml", "compose.identity-data.yml"),
            _container("queue-worker", hash="old"),
        ],
    )
    assert row.status == "drift"
    assert "queue-worker" in row.observed and "postgres" not in row.observed
    assert "WITHOUT overlay compose.identity-data.yml, compose.identity.yml" in row.observed


def test_stack_env_only_drift_is_seen(overlay_repo, monkeypatch):
    # The gap the overlay comparison could not close: every overlay is on the container, yet a
    # value the mode interpolates changed — the hash moved, so `up` would recreate it.
    devmode.set_mode({"gcp": "sa"})
    identity = ("compose.identity.yml", "compose.identity-data.yml")
    (row,) = _stack_rows(
        monkeypatch,
        [_container("queue-worker", *identity, hash="old"), _container("postgres")],
    )
    assert row.status == "drift"
    assert "queue-worker: its rendered config changed" in row.observed
    assert "postgres" not in row.observed and "`fy up` recreates" in row.observed


def test_stack_matching_hashes_are_ok_whatever_the_labels_say(overlay_repo, monkeypatch):
    # postgres predates the identity overlays and app still carries storage-staging, but their
    # hashes match a fresh render — compose would recreate nothing, so nothing is drift.
    devmode.set_mode({"gcp": "sa"})
    (row,) = _stack_rows(
        monkeypatch,
        [
            _container("postgres", "compose.storage-staging.yml"),
            _container("app", "compose.storage-staging.yml"),
        ],
    )
    assert row.status == "ok", row.observed
    assert row.observed == "2 containers match the rendered config"


def test_stack_container_outside_the_render_is_not_compared(overlay_repo, monkeypatch):
    devmode.set_mode({"gcp": "sa"})
    (row,) = _stack_rows(
        monkeypatch,
        [_container("postgres"), _container("e2e-app", hash="whatever")],
        rendered={"postgres"},
    )
    assert row.status == "ok"
    assert row.observed == "1 containers match the rendered config; 1 not in it (e2e-app)"


def test_stack_without_a_render_falls_back_to_overlays_but_never_to_ok(overlay_repo, monkeypatch):
    # The hash leans on podman-compose internals; if they move, the overlay comparison still
    # reports what it CAN see, and what it can't see is "unknown" — never a ✓ it can't back.
    devmode.set_mode({"gcp": "sa"})
    identity = ("compose.identity.yml", "compose.identity-data.yml")
    _unrendered(monkeypatch)
    from foldyard import reconcile

    def rows(running):
        monkeypatch.setattr(devmode, "ps_labels", lambda *a, **k: running)
        (row,) = reconcile.StackScope().rows(devmode.read())
        return row

    fine = rows([_container("queue-worker", *identity), _container("postgres")])
    assert fine.status == "unknown"
    assert "podman-compose moved" in fine.observed
    stale = rows([_container("queue-worker")])
    assert stale.status == "drift" and "WITHOUT overlay" in stale.observed
    # …and an overlay it can't read still counts as touching every service (conservative).
    (overlay_repo / "compose.identity-data.yml").write_text("services: [not, a, mapping")
    unreadable = rows([_container("postgres", "compose.identity.yml")])
    assert unreadable.status == "drift" and "compose.identity-data.yml" in unreadable.observed


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
