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


def test_host_routes_to_supervisor(monkeypatch):
    called: list[bool] = []
    monkeypatch.setattr(supervisor, "main", lambda restart=False: called.append(restart) or 0)
    result = runner.invoke(cli.app, ["host"])
    assert result.exit_code == 0 and called == [False]
    result = runner.invoke(cli.app, ["host", "--restart"])
    assert result.exit_code == 0 and called == [False, True]  # -r forces a holder bounce


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
        allowlist, "grant", lambda h, lvl, ttl: granted.append((h, lvl, ttl)) or {"allow": [h]}
    )
    monkeypatch.setattr(allowlist, "effective", lambda: {"default_deny": True, "allow": ["a.test"]})
    monkeypatch.setattr(allowlist, "revoke", lambda h: {"allow": []})
    assert runner.invoke(cli.app, ["allow", "add", "x.test", "--level", "permanent"]).exit_code == 0
    assert granted == [("x.test", "permanent", None)]
    out = runner.invoke(cli.app, ["allow", "list"])
    assert out.exit_code == 0 and "a.test" in out.output and "default_deny: on" in out.output
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
