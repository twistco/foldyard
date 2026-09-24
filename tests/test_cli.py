"""cli.py — the typer app: verb routing, flags, exit-code propagation. The heavy
backends (devmode/supervisor/tui) are stubbed; we assert the CLI dispatches to them
with the right argv."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foldyard import cli, devmode, supervisor

runner = CliRunner()


@pytest.fixture
def spy_devmode(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(devmode, "main", lambda argv: calls.append(list(argv)) or 0)
    return calls


def test_no_args_shows_help():
    result = runner.invoke(cli.app, [])
    assert "Usage" in result.output
    assert "mode" in result.output and "doctor" in result.output


def test_help_keeps_bracketed_config_tables():
    """Help text names config tables as `[claude]`/`[proxy]`; under typer's default "rich" markup
    those parse as style tags and vanish ("Needs `` in foldyard.toml"). Every sub-app must render
    help as markdown, where brackets are literal."""
    assert "[claude]" in runner.invoke(cli.app, ["claude", "--help"]).output
    assert "[proxy]" in runner.invoke(cli.app, ["allow", "--help"]).output
    for sub in cli.app.registered_groups:
        assert sub.typer_instance is not None
        assert sub.typer_instance.rich_markup_mode == "markdown", sub.name


def test_mode_no_args_routes_to_show(spy_devmode):
    result = runner.invoke(cli.app, ["mode"])
    assert result.exit_code == 0 and spy_devmode == [["show"]]


def test_mode_with_spec_routes_to_set(spy_devmode):
    result = runner.invoke(cli.app, ["mode", "gcp=logs", "github=app", "ttl=1h"])
    assert result.exit_code == 0
    assert spy_devmode == [["set", "gcp=logs", "github=app", "ttl=1h"]]


def test_env_routes(spy_devmode):
    result = runner.invoke(cli.app, ["env"])
    assert result.exit_code == 0 and spy_devmode == [["env"]]


def test_build_routes_services_and_profiles(monkeypatch):
    from foldyard import stack

    calls: list[tuple[list[str], list[str] | None]] = []
    monkeypatch.setattr(
        stack,
        "build",
        lambda services, *, extra_profiles=None: calls.append((services, extra_profiles)) or 0,
    )

    result = runner.invoke(cli.app, ["build", "--profile", "e2e", "e2e-app"])
    assert result.exit_code == 0
    assert calls == [(["e2e-app"], ["e2e"])]


def test_reclaim_routes_to_the_unconditional_sweep(monkeypatch):
    from foldyard import stack

    called: list[bool] = []
    monkeypatch.setattr(stack, "reclaim_now", lambda: called.append(True) or 0)

    result = runner.invoke(cli.app, ["reclaim"])
    assert result.exit_code == 0
    assert called == [True]


def test_hidden_commands_absent_from_help():
    # env/workspaces are hidden=True: not advertised in --help, but callable. Assert on the
    # rendered text directly rather than filtering by the "│" box char — rich downgrades/omits
    # box-drawing depending on the terminal + its version (it bit us in CI), and `mode`/`doctor`
    # only ever appear as commands here, never in prose.
    out = runner.invoke(cli.app, ["--help"]).output
    assert "mode" in out and "doctor" in out  # visible commands advertised
    assert "workspaces" not in out  # hidden command not advertised


def test_doctor_plain_and_deep(spy_devmode):
    assert runner.invoke(cli.app, ["doctor"]).exit_code == 0
    assert runner.invoke(cli.app, ["doctor", "--deep"]).exit_code == 0
    assert spy_devmode == [["doctor"], ["doctor", "deep"]]


def test_workspaces_routes(spy_devmode):
    result = runner.invoke(cli.app, ["workspaces"])
    assert result.exit_code == 0 and spy_devmode == [["workspaces"]]


def test_host_verbs_route_to_the_supervisor(monkeypatch):
    called: list = []
    monkeypatch.setattr(supervisor, "status", lambda: called.append("status") or 0)
    monkeypatch.setattr(supervisor, "restart", lambda: called.append("restart") or 0)
    monkeypatch.setattr(supervisor, "main", lambda: called.append("run") or 0)
    monkeypatch.setattr(
        supervisor, "logs", lambda lines, follow: called.append(("logs", lines, follow)) or 0
    )
    for argv in (["host"], ["host", "status"], ["host", "restart"], ["host", "run"]):
        assert runner.invoke(cli.app, argv).exit_code == 0, argv
    assert runner.invoke(cli.app, ["host", "logs", "-n", "5", "-f"]).exit_code == 0
    assert called == ["status", "status", "restart", "run", ("logs", 5, True)]


def test_host_has_no_foreground_or_stop(monkeypatch):
    # The foreground run is gone (`run` is the hidden process entry, not a verb to hunt a
    # terminal for), and there is no `stop`: a VM without its supervisor refuses all egress.
    monkeypatch.setattr(supervisor, "status", lambda: 0)
    assert runner.invoke(cli.app, ["host", "--restart"]).exit_code != 0
    assert runner.invoke(cli.app, ["host", "stop"]).exit_code != 0
    help_text = runner.invoke(cli.app, ["host", "--help"]).output
    assert "restart" in help_text and "logs" in help_text and " run " not in help_text


def test_tui_routes(monkeypatch):
    from foldyard import tui

    called: list[bool] = []
    monkeypatch.setattr(tui, "main", lambda: called.append(True) or 0)
    result = runner.invoke(cli.app, ["tui"])
    assert result.exit_code == 0 and called == [True]


def test_open_routes_to_browser(monkeypatch):
    from foldyard import browser

    called: list[bool] = []
    monkeypatch.setattr(browser, "open_app", lambda: called.append(True) or 0)
    result = runner.invoke(cli.app, ["open"])
    assert result.exit_code == 0 and called == [True]


def test_unknown_command_fails():
    result = runner.invoke(cli.app, ["bogus"])
    assert result.exit_code != 0


def test_exit_code_propagates(monkeypatch):
    monkeypatch.setattr(devmode, "main", lambda argv: 3)
    result = runner.invoke(cli.app, ["mode"])
    assert result.exit_code == 3


# ── worktree resolution at CLI startup (config + stack must agree on which checkout) ─────


def _callback_ctx():
    """A context for calling the app callback directly.

    Built from typer's own `get_command`, not `click.Command`: typer vendors its own click, so
    a plain click Command is a different type to the one `typer.Context` expects. Its
    `invoked_subcommand` is None — "no verb" — which is what these tests want: they exercise
    the worktree pin, not the version window."""
    import typer
    from typer.main import get_command

    return typer.Context(get_command(cli.app))


def test_callback_pins_worktree_from_cwd_inference(monkeypatch):
    # On the host, posture (config) reads WORKTREE while the stack infers it from CWD. The callback
    # resolves it ONCE via the stack's git+CWD logic and exports it, so both agree (else a host
    # `fy mode` from a worktree would write MAIN's posture — the "changes both" footgun).
    from foldyard import config, stack

    monkeypatch.delenv("WORKTREE", raising=False)
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(stack, "main_repo", lambda: __import__("pathlib").Path("/repo"))
    monkeypatch.setattr(stack, "worktrees_root", lambda m: __import__("pathlib").Path("/repo-wt"))
    monkeypatch.setattr(stack, "_active_worktree", lambda wt_root: "feat")
    cli._resolve_worktree(_callback_ctx())
    assert __import__("os").environ["WORKTREE"] == "feat"


def test_callback_treats_empty_worktree_as_unset(monkeypatch):
    from foldyard import config, stack

    monkeypatch.setenv("WORKTREE", "")
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(stack, "main_repo", lambda: __import__("pathlib").Path("/repo"))
    monkeypatch.setattr(stack, "worktrees_root", lambda m: __import__("pathlib").Path("/repo-wt"))
    monkeypatch.setattr(stack, "_active_worktree", lambda wt_root: "feat")
    cli._resolve_worktree(_callback_ctx())
    assert __import__("os").environ["WORKTREE"] == "feat"


def test_callback_leaves_explicit_worktree_untouched(monkeypatch):
    monkeypatch.setenv("WORKTREE", "other")
    cli._resolve_worktree(
        _callback_ctx()
    )  # already set (box exports it / explicit) → never overridden
    assert __import__("os").environ["WORKTREE"] == "other"


def test_callback_is_a_noop_in_the_box(monkeypatch):
    from foldyard import config

    monkeypatch.delenv("WORKTREE", raising=False)
    monkeypatch.setattr(config, "in_box", lambda: True)  # the box must not infer/escalate
    cli._resolve_worktree(_callback_ctx())
    assert __import__("os").environ.get("WORKTREE") is None


def test_callback_swallows_non_git_fallback(monkeypatch):
    # From a non-git dir, stack.main_repo() raises SystemExit(1) — which is NOT an Exception. The
    # best-effort pin must swallow it (not abort the fronted command) and leave WORKTREE unset.
    from foldyard import config, stack

    monkeypatch.delenv("WORKTREE", raising=False)
    monkeypatch.setattr(config, "in_box", lambda: False)

    def boom():
        raise SystemExit(1)

    monkeypatch.setattr(stack, "main_repo", boom)
    cli._resolve_worktree(_callback_ctx())  # must not raise
    assert __import__("os").environ.get("WORKTREE") is None


def test_allow_verbs_are_the_replacement_for_a_committed_allowlist(monkeypatch, capsys):
    # `[proxy] allow` is gone (repo config the box can write is not an allowlist), so permanent
    # grants need a CLI path as well as the TUI — else the only way to add one is a Textual UI.
    from foldyard import allowlist

    granted: list = []
    monkeypatch.setattr(
        allowlist,
        "grant",
        lambda h, lvl, ttl, build=False: granted.append((h, lvl, ttl, build)) or {"allow": [h]},
    )
    monkeypatch.setattr(
        allowlist,
        "effective",
        lambda: {"default_deny": True, "allow": ["a.test"], "build_allow": ["b.test"]},
    )
    monkeypatch.setattr(allowlist, "revoke", lambda h: {"allow": []})
    assert runner.invoke(cli.app, ["allow", "add", "x.test", "--level", "permanent"]).exit_code == 0
    assert granted == [("x.test", "permanent", None, False)]
    assert runner.invoke(cli.app, ["allow", "add", "y.test", "--build"]).exit_code == 0
    assert granted[-1] == ("y.test", "session", None, True)  # for image builds only
    out = runner.invoke(cli.app, ["allow", "list"])
    assert out.exit_code == 0 and "a.test" in out.output and "default_deny: on" in out.output
    assert "b.test" in out.output and "builds only" in out.output
    assert runner.invoke(cli.app, ["allow", "remove", "x.test"]).exit_code == 0


def test_allow_sync_reports_nothing_pending(tmp_path, monkeypatch):
    # A project with neither `[proxy] recommend` nor a declared agent has nothing to offer. Pinned
    # against a SCRATCH checkout + state dir: foldyard's own config declares [claude]/[codex],
    # whose plugins recommend their installer hosts, and a real state dir would be the host's store.
    from foldyard import config

    (tmp_path / "foldyard.toml").write_text('[project]\nname = "scratch"\n')
    monkeypatch.setenv("FOLDYARD_REPO", str(tmp_path))
    monkeypatch.setenv("FOLDYARD_STATE_DIR", str(tmp_path / "state"))
    config.clear_caches()
    result = runner.invoke(cli.app, ["allow", "sync"])
    config.clear_caches()
    assert result.exit_code == 0
    assert "nothing pending" in result.output


def _scratch(tmp_path, monkeypatch, toml: str = '[project]\nname = "scratch"\n[proxy]\n'):
    """A scratch checkout + host state + log dir, host-side — a real allow-store, no live one."""
    from foldyard import config

    (tmp_path / "foldyard.toml").write_text(toml)
    monkeypatch.setenv("FOLDYARD_REPO", str(tmp_path))
    monkeypatch.setenv("FOLDYARD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FOLDYARD_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(config, "in_box", lambda: False)
    config.clear_caches()
    return tmp_path / "logs" / "egress.jsonl"


def test_wall_learn_then_review_grants_the_recorded_hosts(tmp_path, monkeypatch):
    # The loop the feature exists for: open a window, the proxy records what it would refuse,
    # the review grants it in one batch and prints the lines that share it with the team.
    import json
    from datetime import UTC, datetime

    from foldyard import allowlist, config

    log = _scratch(tmp_path, monkeypatch)
    try:
        result = runner.invoke(cli.app, ["allow", "enforce", "learn", "--for", "30m"])
        assert result.exit_code == 0 and "LEARNING until" in result.output
        assert allowlist.learning() is not None and allowlist.default_deny() is False
        listed = runner.invoke(cli.app, ["allow", "list"])
        assert "LEARNING until" in listed.output

        now = datetime.now(UTC).isoformat(timespec="seconds")
        log.parent.mkdir(parents=True)
        rows = [
            {"ts": now, "host": "registry.npmjs.org", "would_block": True, "ua": "npm/10.8.2 x"},
            {"ts": now, "host": "registry.npmjs.org", "method": "GET", "path": "/react?t=x"},
            {"ts": now, "host": "seen.example.com", "status": 200},  # passed, not would-block
        ]
        log.write_text("".join(json.dumps(r) + "\n" for r in rows))

        result = runner.invoke(cli.app, ["allow", "learn", "--yes"])
        assert result.exit_code == 0, result.output
        assert "registry.npmjs.org" in result.output and "npm/10.8.2" in result.output
        assert "seen.example.com" not in result.output
        assert "fetched /react" in result.output and "t=x" not in result.output
        assert (
            '{ host = "registry.npmjs.org", why = "observed: npm/10.8.2 GET /react — edit me" }'
            in result.output
        )
        assert "replace it with the reason" in result.output
        assert "registry.npmjs.org" in allowlist.live_hosts()
        # Granted now, so a second review has nothing left to offer.
        again = runner.invoke(cli.app, ["allow", "learn"])
        assert "nothing to grant" in again.output
    finally:
        config.clear_caches()


def test_allow_sync_points_at_refused_hosts_when_nothing_is_recommended(tmp_path, monkeypatch):
    # "nothing pending" was read as "nothing to answer" while the TUI's Network Log listed refused
    # hosts to accept — those aren't recommendations, so name them and where they're answered
    import json
    from datetime import UTC, datetime

    from foldyard import allowlist, config

    log = _scratch(tmp_path, monkeypatch)
    try:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        log.parent.mkdir(parents=True)
        rows = [
            {"ts": now, "host": "deb.example.org", "blocked": True, "status": 403},
            {"ts": now, "host": "deb.example.org", "blocked": True, "status": 403},
            {"ts": now, "host": "pypi.example.org", "would_block": True},
            {"ts": now, "host": "granted.example.org", "blocked": True},
            {"ts": now, "host": "ok.example.org", "status": 200},
        ]
        log.write_text("".join(json.dumps(r) + "\n" for r in rows))
        allowlist.grant("granted.example.org", "permanent")
        result = runner.invoke(cli.app, ["allow", "sync"])
        assert result.exit_code == 0, result.output
        assert "nothing pending" in result.output
        assert "2 hosts the allowlist refused" in result.output
        assert "deb.example.org, pypi.example.org" in result.output
        assert "granted.example.org" not in result.output
    finally:
        config.clear_caches()


def test_allow_learn_without_a_window_says_how_to_start_one(tmp_path, monkeypatch):
    from foldyard import config

    _scratch(tmp_path, monkeypatch)
    try:
        result = runner.invoke(cli.app, ["allow", "learn"])
        assert result.exit_code == 0 and "fy allow enforce learn" in result.output
    finally:
        config.clear_caches()


def test_wall_learn_rejects_a_bad_duration(tmp_path, monkeypatch):
    from foldyard import allowlist, config

    _scratch(tmp_path, monkeypatch)
    try:
        result = runner.invoke(cli.app, ["allow", "enforce", "learn", "--for", "0s"])
        assert result.exit_code != 0 and allowlist.learning() is None
    finally:
        config.clear_caches()


def test_renamed_verbs_keep_their_old_names_as_hidden_aliases(tmp_path, monkeypatch):
    # `fy allow wall` → `fy allow enforce`, `fy machine host-wall` → `host-firewall`: a script or a
    # teammate's muscle memory still works, and `--help` shows only the new name.
    from foldyard import allowlist, config

    _scratch(tmp_path, monkeypatch)
    try:
        result = runner.invoke(cli.app, ["allow", "wall", "off"])
        assert result.exit_code == 0 and allowlist.default_deny() is False
    finally:
        config.clear_caches()
    for sub, old, new in (("allow", "wall", "enforce"), ("machine", "host-wall", "host-firewall")):
        help_text = runner.invoke(cli.app, [sub, "--help"]).output
        assert new in help_text and f" {old} " not in help_text
        assert runner.invoke(cli.app, [sub, old, "--help"]).exit_code == 0


# ── `fy --version` ────────────────────────────────────────────────────────────


def test_version_flag_prints_the_installed_version():
    """`fy --version` exists and answers. It is the groundwork for `[project].min_foldyard_version`
    and for doctor's box/host drift row: before this, a running foldyard could not name itself, so
    "which version is this?" had no answer at all — on the Mac, in the box, or in a bug report."""
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == metadata.version("foldyard")


def test_version_is_single_sourced_from_the_install_metadata():
    """``__version__`` must not be a SECOND hand-maintained copy of ``[project].version``. It was
    one (hardcoded ``0.0.1``) while ``box.py`` separately read ``importlib.metadata`` — two sources
    that drift silently at the first release bump, and the one the box pins itself to is the one
    nobody edits."""
    import foldyard

    assert foldyard.__version__ == metadata.version("foldyard")


def test_importing_foldyard_does_not_read_install_metadata():
    """The recipe hot path imports the package on every `just` recipe, so the version lookup stays
    LAZY (PEP 562 ``__getattr__``) — attribute access pays for it, plain import does not."""
    src = (Path(cli.__file__).parent / "__init__.py").read_text()
    assert "def __getattr__" in src
    assert "from importlib import metadata" not in src.split("def __getattr__")[0]
