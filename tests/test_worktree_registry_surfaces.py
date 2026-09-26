"""The registry's user-facing edges: a launch verb in an unregistered worktree is refused with the
fix in the message, and a worktree the host ignores is never silent (doctor row + supervisor log).
"Reporting is part of the fix" (CLAUDE.md): a worktree that went quiet must say why."""

from __future__ import annotations

import pytest

from foldyard import config, configpin, devmode, supervisor


@pytest.fixture
def host(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    (main / ".git").mkdir(parents=True)
    root = tmp_path / "repo-worktrees"
    root.mkdir()
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(devmode, "in_box", lambda: False)
    monkeypatch.setattr(devmode, "main_repo", lambda: main)
    monkeypatch.setattr(config, "worktrees_root", lambda _base: root)
    return main, root


def _stray(root, name):
    (root / name / ".git").mkdir(parents=True)


def test_a_launch_in_an_unregistered_worktree_is_refused_with_the_fix(host, monkeypatch):
    _main, root = host
    _stray(root, "old")
    monkeypatch.setattr(config, "active_worktree", lambda: "old")
    with pytest.raises(SystemExit, match=r"fy worktree add old"):
        configpin.gate("fy up")


def test_a_launch_in_a_registered_worktree_is_not_refused_for_it(
    host, monkeypatch, register_worktree
):
    main, root = host
    register_worktree(main, "feat", root / "feat")
    monkeypatch.setattr(config, "active_worktree", lambda: "feat")
    monkeypatch.setattr(configpin, "resolve", lambda *a, **k: "clean")
    assert configpin.gate("fy up") == "clean"


def test_doctor_names_the_worktrees_the_host_ignores(host, register_worktree):
    main, root = host
    register_worktree(main, "known", root / "known")
    _stray(root, "old")
    status, name, detail = devmode._worktree_registry_check()
    assert (status, name) == ("warn", "worktrees")
    assert "old" in detail and "fy worktree add old" in detail and "known" not in detail


def test_doctor_is_green_when_every_worktree_is_registered(host, register_worktree):
    main, root = host
    register_worktree(main, "known", root / "known")
    status, _name, detail = devmode._worktree_registry_check()
    assert status == "ok" and "1 registered" in detail


def test_supervisor_logs_ignored_worktrees_once_per_change(host, monkeypatch):
    _main, root = host
    logged: list[str] = []
    monkeypatch.setattr(supervisor, "log", logged.append)
    monkeypatch.setattr(supervisor, "_unregistered_seen", [])
    _stray(root, "old")
    supervisor._report_unregistered()
    supervisor._report_unregistered()
    assert len(logged) == 1 and "old" in logged[0] and "fy worktree add old" in logged[0]
    _stray(root, "older")
    supervisor._report_unregistered()
    assert len(logged) == 2 and "older" in logged[1]
