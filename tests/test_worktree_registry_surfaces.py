"""The registry's user-facing edges: a launch verb in an unregistered worktree is refused with the
fix in the message, and a worktree the host ignores is never silent (doctor row + supervisor log).
"Reporting is part of the fix" (docs/dev/invariants.md): a worktree that went quiet must say why."""

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


# The box names those dirs: each name reaches your terminal (and the log) with its control
# characters shown as escapes, so none of them acts on it.
UNPRINTABLE = "\x1b]52;c;aGk=\x07\x9b2J\N{RIGHT-TO-LEFT OVERRIDE}"  # OSC 52, a C1 CSI, a bidi flip
CONTROLS = {"\x1b", "\x07", "\x9b", "\N{RIGHT-TO-LEFT OVERRIDE}"}


def test_doctor_shows_a_box_chosen_dir_name_inert(host):
    _main, root = host
    _stray(root, f"old{UNPRINTABLE}")
    _status, _name, detail = devmode._worktree_registry_check()
    assert "old\\x1b]52" in detail and not CONTROLS & set(detail)


def test_doctor_shows_a_box_made_worktrees_name_inert(host, monkeypatch):
    from foldyard import boxworktree

    monkeypatch.setattr(boxworktree, "names", lambda _main: [f"boxed{UNPRINTABLE}"])
    _status, _name, detail = devmode._worktree_registry_check()
    assert "boxed\\x1b]52" in detail and not CONTROLS & set(detail)


def test_the_supervisor_logs_a_box_chosen_dir_name_inert(host, monkeypatch):
    _main, root = host
    logged: list[str] = []
    monkeypatch.setattr(supervisor, "log", logged.append)
    monkeypatch.setattr(supervisor, "_unregistered_seen", [])
    _stray(root, f"old{UNPRINTABLE}")
    supervisor._report_unregistered()
    assert "old\\x1b]52" in logged[0] and not CONTROLS & set(logged[0])


def test_mode_workspaces_shows_box_chosen_names_inert(monkeypatch, capsys):
    ws = {
        "name": f"old{UNPRINTABLE}",
        "branch": f"wt/old{UNPRINTABLE}",
        "path": f"/repo-worktrees/old{UNPRINTABLE}",
        "containers": 0,
        "devbox": False,
    }
    monkeypatch.setattr(devmode, "workspaces", lambda: [ws])
    assert devmode.main(["workspaces"]) == 0
    out = capsys.readouterr().out
    assert out.count("old\\x1b]52") == 3 and not CONTROLS & set(out)


def test_main_repo_is_the_main_checkout_even_when_foldyard_repo_names_a_worktree(
    tmp_path, fresh_config
):
    # Consumer recipes (and the e2e runner) export FOLDYARD_REPO=<the checkout they run in>. From a
    # worktree that is the WORKTREE — and the registry, written by `fy worktree add` from main, is
    # keyed by main. Both sides must agree on main, or a registered worktree reads as unregistered.
    import subprocess

    main = tmp_path / "repo"
    main.mkdir()
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(main)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "c"], check=True)
    feat = tmp_path / "repo-worktrees" / "feat"
    subprocess.run([*git, "worktree", "add", "-q", str(feat)], check=True)

    fresh_config(FOLDYARD_REPO=feat)
    assert devmode.main_repo().resolve() == main.resolve()
    fresh_config(FOLDYARD_REPO=main)
    assert devmode.main_repo().resolve() == main.resolve()


def test_main_repo_outside_git_is_the_checkout_itself(tmp_path, fresh_config):
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert devmode.main_repo() == config.repo_root()
