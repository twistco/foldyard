"""The ambient config channel on the host is the ADOPTED snapshot (ADR-0022: the channel, not the
field). Every ``config.X()`` read outside a bound worktree config — ``[machine].firewall`` /
``host_firewall`` / ``name`` / ``worktrees_root``, verify's exemptions, every un-gated verb — used
to read the working tree the box writes. These tests run the REAL host read; the rest of the suite
reads the tree through conftest's ``ambient_reads_the_tree`` seam, as it was written to."""

from __future__ import annotations

import os
import sys

import pytest

from foldyard import config, configpin, machine, preflight

# Captured at import, before conftest's autouse seam swaps it for the tree reader.
_REAL_HOST_TOML = config._host_toml

ADOPTED = '[project]\nname = "acme"\n\n[machine]\nfirewall = true\nhost_firewall = true\n'
FLIPPED = '[project]\nname = "acme"\n\n[machine]\nfirewall = false\nhost_firewall = false\n'


@pytest.fixture
def host_checkout(tmp_path, fresh_config, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "foldyard.toml").write_text(ADOPTED)
    monkeypatch.setattr(config, "_host_toml", _REAL_HOST_TOML)
    monkeypatch.setattr(config, "in_box", lambda: False)
    fresh_config(
        FOLDYARD_REPO=root, MACHINE_FIREWALL=None, MACHINE_WALL=None, MACHINE_HOST_FIREWALL=None
    )
    return root


def _adopt(root):
    configpin.adopt(config.resolve(worktree="", repo=root))
    config.clear_caches()


def test_the_host_reads_what_it_adopted_not_what_the_box_wrote(host_checkout):
    _adopt(host_checkout)
    (host_checkout / "foldyard.toml").write_text(FLIPPED)  # the box turns the walls off
    config.clear_caches()
    assert config.machine_wall() is True
    assert config.machine_host_wall() is True
    assert config.ambient_adopted() is True


def test_nothing_adopted_reads_as_nothing_on_the_host(host_checkout):
    assert config.project() == "repo"  # not the tree's "acme": the tree isn't read at all
    assert config.machine_wall() is False
    assert config.ambient_adopted() is False


def test_the_box_reads_its_own_checkout(host_checkout, monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    (host_checkout / "foldyard.toml").write_text(FLIPPED)
    config.clear_caches()
    assert config.machine_wall() is False  # the tree, as written
    assert config.ambient_adopted() is True  # nothing to adopt in the box


def test_machine_ensure_refuses_a_checkout_with_nothing_adopted(host_checkout, monkeypatch):
    # An empty config is NOT a safe floor here: `[machine].firewall` defaults off, so provisioning
    # from it would boot the VM unwalled. Whatever verb got here, it stops.
    booted: list[str] = []
    monkeypatch.setattr(machine.BACKEND, "available", lambda: True)
    monkeypatch.setattr(machine, "exists", lambda: booted.append("probe") or True)
    with pytest.raises(SystemExit):
        machine.ensure(host_checkout, host_checkout.parent / "repo-worktrees")
    assert booted == []


def test_preflight_runs_the_gate_before_anything_else(host_checkout, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(configpin, "gate", lambda verb: order.append(f"gate:{verb}") or "clean")
    monkeypatch.setattr(preflight, "issues", lambda: order.append("issues") or [])
    preflight.check_or_abort("fy up")
    assert order == ["gate:fy up", "issues"]


@pytest.fixture
def adopting_gate(host_checkout, monkeypatch):
    """The gate on a terminal, first sight, the operator pressing Enter; exec recorded."""
    from foldyard import devmode

    monkeypatch.setattr(config, "active_worktree", lambda: "")
    monkeypatch.setattr(devmode, "main_repo", lambda: host_checkout)
    monkeypatch.setattr(configpin.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a: "")
    monkeypatch.delenv(configpin.RERUN_ENV, raising=False)
    execs: list[list[str]] = []
    monkeypatch.setattr(configpin.os, "execv", lambda exe, argv: execs.append(argv))
    monkeypatch.setattr(sys, "orig_argv", [sys.executable, "/opt/bin/fy", "up"])
    return execs


def test_an_adoption_at_the_gate_reruns_the_command_with_it(adopting_gate):
    # Import-time constants (machine.MACHINE/BACKEND, devmode.AXES…) were bound from the config
    # BEFORE the adoption; only a fresh process sees the adopted one everywhere.
    configpin.gate("fy up")
    assert adopting_gate == [[sys.executable, "/opt/bin/fy", "up"]]
    assert os.environ[configpin.RERUN_ENV] == "1"


def test_the_rerun_never_reruns_again(adopting_gate, monkeypatch):
    monkeypatch.setenv(configpin.RERUN_ENV, "1")
    assert configpin.gate("fy up") == "pinned"
    assert adopting_gate == []
    assert configpin.RERUN_ENV not in os.environ  # consumed: never leaks into children
    assert config.machine_wall() is True  # …and this process now reads the adoption


def test_a_worktree_offset_pin_comes_from_mains_adoption(host_checkout, monkeypatch):
    from foldyard import stack

    monkeypatch.setattr(stack, "main_repo", lambda: host_checkout)
    monkeypatch.delenv("WT_OFFSET", raising=False)
    (host_checkout / "foldyard.local.toml").write_text("[worktree-offsets]\nfeat = 5\n")
    _adopt(host_checkout)
    (host_checkout / "foldyard.local.toml").write_text("[worktree-offsets]\nfeat = 9\n")
    assert stack._pinned_offset("feat") == 5
