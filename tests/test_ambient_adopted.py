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


# ── an unadopted checkout says so, rather than reporting its empty config as the config ─────────
# Tangible's `just e2e` on a fresh host printed "[project].compose is unset in foldyard.toml" and a
# PODMAN_PROJECT guessed from the directory name, while the tree's toml set both: the host read
# nothing (nothing adopted) and reported what nothing implies. Pinning makes an edit silent by
# construction, so the silence itself has to be reported (DEVELOPMENT.md: reporting is part of it).


def test_an_unadopted_checkout_is_named_as_such(host_checkout, monkeypatch):
    notice = config.unadopted_notice()
    assert notice and "isn't adopted" in notice and "`fy config adopt`" in notice
    _adopt(host_checkout)
    assert config.unadopted_notice() is None
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert config.unadopted_notice() is None


def test_no_notice_without_a_tree_config(host_checkout):
    (host_checkout / "foldyard.toml").unlink()
    assert config.unadopted_notice() is None  # nothing to adopt: an empty config IS the config


def test_a_bound_config_is_never_called_unadopted(host_checkout):
    # The supervisor/TUI bind a worktree's config, and they only bind ADOPTED ones
    # (devmode.worktree_config): never report the ambient checkout's state over it.
    bound = config.Config(repo_root=host_checkout, worktree="", toml={})
    with config.using(bound):
        assert config.unadopted_notice() is None


@pytest.mark.parametrize("verb", ["build", "ps", "down", "logs"])
def test_stack_verbs_refuse_rather_than_call_the_stack_undeclared(host_checkout, capsys, verb):
    from foldyard import stack

    assert stack.stack_declared(verb, None) == 1
    out, err = capsys.readouterr()
    assert "unset" not in out + err and "doesn't drive one" not in out + err
    assert f"fy {verb}: nothing adopted" in err


def test_shellenv_refuses_rather_than_emit_a_guessed_env(host_checkout, capsys):
    from foldyard import stack

    assert stack.shellenv() == 1
    out, err = capsys.readouterr()
    assert out.strip() == "exit 1"  # the recipe's eval aborts; no PODMAN_PROJECT, no COMPOSE
    assert "fy shellenv: nothing adopted" in err


def _invoke(argv):
    from typer.testing import CliRunner

    from foldyard import cli

    return CliRunner().invoke(cli.app, argv)


def test_the_cli_says_it_once_up_front(host_checkout):
    result = _invoke(["shellenv"])
    assert result.stdout.strip() == "exit 1"
    assert result.stderr.count("isn't adopted") == 1


@pytest.mark.parametrize("argv", [["docs"], ["config", "status"]])
def test_the_verbs_that_are_about_adoption_stay_quiet(host_checkout, argv):
    # `fy config` IS the adoption surface (status says it in its own words), `fy docs` is the
    # manual; `fy up`/`fy code`/`fy claude` run the gate, which asks or refuses on its own.
    assert "isn't adopted" not in _invoke(argv).stderr
