"""ports.py — the cross-project daemon port-band registry, and config's derivations from it.

The regression these guard: two projects on one Mac each ran a per-project supervisor, but both
derived the same global proxy port (8088), so each supervisor reaped the other's proxy as
"orphaned" every tick — an endless kill loop that also sent one project's box egress through the
OTHER project's proxy (wrong CA, wrong creds). Bands make the ports per-project; the scoped
reaper (test_supervisor) makes any residual collision nag instead of fight.
"""

from __future__ import annotations

import json

import pytest

from foldyard import config, ports


def bound(worktree: str = ""):
    """Bind a minimal named consumer config (mirrors conftest's GENERIC_TOML shape)."""
    toml: dict = {"project": {"name": "generic"}}
    return config.using(config.Config(repo_root=config.repo_root(), worktree=worktree, toml=toml))


def read_registry() -> dict:
    return json.loads(ports.registry_file().read_text())


def test_first_project_gets_first_band():
    assert ports.project_base("alpha") == ports.FIRST_BASE


def test_allocation_is_sticky_and_per_project():
    a1 = ports.project_base("alpha")
    b = ports.project_base("beta")
    a2 = ports.project_base("alpha")
    assert a1 == a2 == ports.FIRST_BASE
    assert b == ports.FIRST_BASE + ports.BAND
    assert read_registry() == {"alpha": a1, "beta": b}


def test_corrupt_registry_recovers():
    ports.registry_file().parent.mkdir(parents=True, exist_ok=True)
    ports.registry_file().write_text("not json{")
    assert ports.project_base("alpha") == ports.FIRST_BASE
    assert read_registry() == {"alpha": ports.FIRST_BASE}


def test_non_int_entries_are_ignored_not_crashed():
    ports.registry_file().parent.mkdir(parents=True, exist_ok=True)
    ports.registry_file().write_text(json.dumps({"weird": "yes", "beta": ports.FIRST_BASE}))
    assert ports.project_base("alpha") == ports.FIRST_BASE + ports.BAND


def test_exhaustion_raises_with_guidance():
    ports.registry_file().parent.mkdir(parents=True, exist_ok=True)
    full = {
        f"p{i}": base
        for i, base in enumerate(range(ports.FIRST_BASE, ports.LAST_BASE + 1, ports.BAND))
    }
    ports.registry_file().write_text(json.dumps(full))
    with pytest.raises(RuntimeError, match=r"ports\.json"):
        ports.project_base("one-too-many")


def test_band_slots_are_disjoint():
    """The proxy and minter spans (each base + the 0..89 worktree offsets) must not overlap —
    the old 9-apart global bases (8079/8088) let one worktree's minter land on another's proxy."""
    assert ports.MINTER_SLOT >= ports.PROXY_SLOT + 90
    assert ports.BAND >= ports.MINTER_SLOT + 90


def test_last_band_stays_below_ephemeral_range():
    assert ports.LAST_BASE + ports.BAND - 1 < 49152


# ── config derivations ──────────────────────────────────────────────────────────────────


def test_config_bases_come_from_the_project_band(monkeypatch):
    monkeypatch.delenv("FY_PROXY_PORT", raising=False)
    monkeypatch.delenv("GCP_MINTER_PORT", raising=False)
    with bound():
        assert config.proxy_port_base() == ports.FIRST_BASE + ports.PROXY_SLOT
        assert config.gcp_minter_port_base() == ports.FIRST_BASE + ports.MINTER_SLOT
        assert read_registry() == {"generic": ports.FIRST_BASE}


def test_env_override_wins_and_skips_allocation(monkeypatch):
    monkeypatch.setenv("FY_PROXY_PORT", "9999")
    monkeypatch.setenv("GCP_MINTER_PORT", "7777")
    with bound():
        assert config.proxy_port_base() == 9999
        assert config.gcp_minter_port_base() == 7777
    assert not ports.registry_file().exists()


def test_in_box_without_pinned_env_falls_back_to_legacy(monkeypatch):
    """A box created before band allocation has no pinned FY_PROXY_PORT/GCP_MINTER_PORT env; its
    in-box derivations keep the legacy bases (deterministic, and it can't read the Mac registry).
    Boxes created now always carry the pinned env (test_box), so this is migration-only."""
    monkeypatch.delenv("FY_PROXY_PORT", raising=False)
    monkeypatch.delenv("GCP_MINTER_PORT", raising=False)
    monkeypatch.setattr(config, "in_box", lambda: True)
    with bound():
        assert config.proxy_port_base() == 8088
        assert config.gcp_minter_port_base() == 8079
    assert not ports.registry_file().exists()


def test_worktree_ports_offset_from_the_band(monkeypatch):
    monkeypatch.delenv("FY_PROXY_PORT", raising=False)
    with bound(worktree="feature-x"):
        offset = config.worktree_offset("feature-x")
        assert 1 <= offset <= 89
        assert config.proxy_port() == ports.FIRST_BASE + offset
