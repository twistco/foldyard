"""compat.py — the declared foldyard version floor and nudge.

The consumer repo declares which foldyard its checkout needs; the gate is pure so the
policy is testable without a config, an install, or a terminal.
"""

from __future__ import annotations

import pytest

from foldyard import compat, config

# ── parsing ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("0.1.0", (0, 1, 0)),
        ("1.2", (1, 2)),
        ("10.0.3", (10, 0, 3)),
        ("0.2.0rc1", (0, 2, 0)),  # pre-releases satisfy their own floor; simpler than PEP 440
        ("v0.3.1", (0, 3, 1)),
    ],
)
def test_parse_accepts_ordinary_versions(raw, want):
    assert compat._parse(raw) == want


@pytest.mark.parametrize("raw", ["0+unknown", "", "nightly", None])
def test_parse_refuses_what_it_cannot_order(raw):
    # __init__ returns "0+unknown" from a bare PYTHONPATH import precisely so a floor does
    # not trust it. Unparseable must mean "cannot tell", never "very old".
    assert compat._parse(raw) is None


# ── the floor (hard) ─────────────────────────────────────────────────────────────────


def test_floor_violation_aborts_and_names_both_versions():
    msg, abort = compat.version_gate("0.1.0", minimum="0.4.0", recommended=None)
    assert abort is True
    assert msg is not None
    assert "0.1.0" in msg
    assert "0.4.0" in msg


def test_floor_met_exactly_is_silent():
    assert compat.version_gate("0.4.0", minimum="0.4.0", recommended=None) == (None, False)


def test_floor_met_by_newer_is_silent():
    assert compat.version_gate("1.0.0", minimum="0.4.0", recommended=None) == (None, False)


def test_no_declaration_is_silent():
    assert compat.version_gate("0.1.0", minimum=None, recommended=None) == (None, False)


def test_floor_beats_nudge_when_both_are_violated():
    msg, abort = compat.version_gate("0.1.0", minimum="0.4.0", recommended="0.9.0")
    assert abort is True
    assert msg is not None
    assert "0.9.0" not in msg  # one instruction, not two


# ── the nudge (soft) ─────────────────────────────────────────────────────────────────


def test_nudge_warns_without_aborting():
    msg, abort = compat.version_gate("0.1.0", minimum=None, recommended="0.4.0")
    assert abort is False
    assert msg is not None
    assert "0.4.0" in msg


def test_nudge_silent_once_satisfied():
    assert compat.version_gate("0.4.0", minimum=None, recommended="0.4.0") == (None, False)


def test_nudge_can_be_squelched_but_the_floor_cannot():
    assert compat.version_gate("0.1.0", None, "0.4.0", quiet_nudge=True) == (None, False)
    _, abort = compat.version_gate("0.1.0", "0.4.0", None, quiet_nudge=True)
    assert abort is True


# ── unknown installed version ────────────────────────────────────────────────────────


def test_unknown_installed_version_never_blocks():
    # A source-tree import reports "0+unknown". Refusing to run would be worse than the
    # risk the floor guards against, and we cannot prove a violation.
    msg, abort = compat.version_gate("0+unknown", minimum="0.4.0", recommended=None)
    assert abort is False
    assert msg is not None and "0.4.0" in msg


def test_unparseable_declaration_is_ignored():
    # A typo in the consumer's foldyard.toml must not brick every fy invocation.
    assert compat.version_gate("0.1.0", minimum="not-a-version", recommended=None) == (None, False)


# ── the fix instruction differs by where you are ─────────────────────────────────────


def test_fix_command_on_the_mac_is_a_uv_reinstall():
    msg, _ = compat.version_gate("0.1.0", minimum="0.4.0", recommended=None, in_box=False)
    assert msg is not None
    assert "uv tool install" in msg
    assert "fy box up" not in msg


def test_fix_command_in_the_box_is_a_box_rebuild():
    # The box's foldyard is installed by the bootstrap to match the host, so `uv tool install`
    # inside the box would be undone by the next `fy box up` — and the box has no egress for it.
    msg, _ = compat.version_gate("0.1.0", minimum="0.4.0", recommended=None, in_box=True)
    assert msg is not None
    assert "fy box up" in msg
    assert "uv tool install" not in msg


# ── config accessors ─────────────────────────────────────────────────────────────────


def test_accessors_read_the_project_table(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text(
        '[project]\nname = "p"\nmin_foldyard_version = "0.4.0"\n'
        'recommended_foldyard_version = "0.6.0"\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.min_foldyard_version() == "0.4.0"
    assert config.recommended_foldyard_version() == "0.6.0"


def test_accessors_are_none_when_undeclared(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "p"\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.min_foldyard_version() is None
    assert config.recommended_foldyard_version() is None


def test_accessors_ignore_non_string_declarations(fresh_config, tmp_path):
    # TOML floats: `min_foldyard_version = 0.4` parses as a float, not "0.4".
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "p"\nmin_foldyard_version = 0.4\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.min_foldyard_version() is None


# ── wiring: the gate fronts every verb ───────────────────────────────────────────────


@pytest.fixture
def repo_declaring(fresh_config, tmp_path, monkeypatch):
    """A checkout whose foldyard.toml declares a version window."""

    def _make(**keys):
        decl = "".join(f'{k} = "{v}"\n' for k, v in keys.items())
        (tmp_path / "foldyard.toml").write_text(f'[project]\nname = "p"\n{decl}')
        fresh_config(FOLDYARD_REPO=tmp_path)
        monkeypatch.chdir(tmp_path)
        return tmp_path

    return _make


def test_gate_blocks_a_verb_when_the_floor_is_unmet(repo_declaring, monkeypatch, capsys):
    repo_declaring(min_foldyard_version="99.0.0")
    with pytest.raises(SystemExit) as exc:
        compat.gate_or_abort()
    assert exc.value.code == 1
    assert "99.0.0" in capsys.readouterr().err


def test_gate_is_silent_when_nothing_is_declared(repo_declaring, capsys):
    repo_declaring()
    compat.gate_or_abort()
    assert capsys.readouterr().err == ""


def test_gate_nudges_without_aborting(repo_declaring, capsys):
    repo_declaring(recommended_foldyard_version="99.0.0")
    compat.gate_or_abort("up")  # must not raise
    assert "99.0.0" in capsys.readouterr().err


def test_gate_never_becomes_the_thing_that_breaks_fy(repo_declaring, monkeypatch, capsys):
    # A version check is infrastructure for everything else; if it throws, it must lose.
    repo_declaring(min_foldyard_version="99.0.0")
    monkeypatch.setattr(config, "min_foldyard_version", lambda: 1 / 0)
    compat.gate_or_abort()
    assert capsys.readouterr().err == ""


def test_version_flag_still_answers_under_an_unmet_floor(repo_declaring):
    # The one question that must keep working when you are being told to upgrade is
    # "what have I got?" — --version is eager, so it exits before the callback body.
    from typer.testing import CliRunner

    from foldyard import cli

    repo_declaring(min_foldyard_version="99.0.0")
    result = CliRunner().invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert "99.0.0" not in result.output


# ── wiring: the doctor row ───────────────────────────────────────────────────────────


def _row(repo_declaring, **keys):
    from foldyard import devmode

    repo_declaring(**keys)
    return list(devmode._version_window_check())


def test_doctor_says_nothing_when_the_repo_has_no_opinion(repo_declaring):
    assert _row(repo_declaring) == []


def test_doctor_fails_the_row_on_an_unmet_floor(repo_declaring):
    (status, name, detail) = _row(repo_declaring, min_foldyard_version="99.0.0")[0]
    assert (status, name) == ("fail", "foldyard version")
    assert "99.0.0" in detail


def test_doctor_warns_on_an_unmet_recommendation(repo_declaring):
    (status, _, detail) = _row(repo_declaring, recommended_foldyard_version="99.0.0")[0]
    assert status == "warn"
    assert "99.0.0" in detail


def test_doctor_is_ok_inside_the_window(repo_declaring):
    (status, _, _) = _row(repo_declaring, min_foldyard_version="0.0.1")[0]
    assert status == "ok"


def test_doctor_reports_the_nudge_even_when_silenced(repo_declaring, monkeypatch):
    # FOLDYARD_NO_VERSION_NUDGE silences the per-invocation nag, not an explicit `fy doctor`.
    monkeypatch.setenv("FOLDYARD_NO_VERSION_NUDGE", "1")
    (status, _, _) = _row(repo_declaring, recommended_foldyard_version="99.0.0")[0]
    assert status == "warn"


# ── the nudge is scoped to session-starting verbs ────────────────────────────────────


@pytest.mark.parametrize("verb", sorted(compat.NUDGE_VERBS))
def test_nudge_fires_on_session_starting_verbs(repo_declaring, capsys, verb):
    repo_declaring(recommended_foldyard_version="99.0.0")
    compat.gate_or_abort(verb)
    assert "99.0.0" in capsys.readouterr().err


@pytest.mark.parametrize("verb", ["ps", "mode", "logs", "shell", "open", "state"])
def test_nudge_stays_quiet_on_verbs_you_run_all_day(repo_declaring, capsys, verb):
    # A warning on every invocation is filtered out by the reader within a day, and takes
    # foldyard's other stderr with it. The nudge is worth having only if it stays rare.
    repo_declaring(recommended_foldyard_version="99.0.0")
    compat.gate_or_abort(verb)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("verb", ["ps", "mode", None])
def test_the_floor_ignores_the_verb_entirely(repo_declaring, capsys, verb):
    # Scoping is a noise concession for the nudge alone. A stale fy misreads the config that
    # drives every verb, so the refusal cannot be one of them.
    repo_declaring(min_foldyard_version="99.0.0")
    with pytest.raises(SystemExit):
        compat.gate_or_abort(verb)
    assert "99.0.0" in capsys.readouterr().err


# ── the seam: the nudge has to survive the trip through typer ────────────────────────


def _invoke(argv):
    """Run the real app through its runner, capturing stderr separately."""
    from typer.testing import CliRunner

    from foldyard import cli

    return CliRunner().invoke(cli.app, argv)


def test_nudge_reaches_the_cli_not_just_the_unit(repo_declaring):
    # Regression: the verb was first read via click.get_current_context(), but typer vendors
    # its own click — the top-level package reads a DIFFERENT context stack and always answered
    # None, so the nudge could never fire in a real invocation while every unit test passed.
    # Assert through the seam, with a verb, or this class of bug is invisible again.
    repo_declaring(recommended_foldyard_version="99.0.0")
    result = _invoke(["up", "--help"])
    assert "99.0.0" in result.output


def test_no_nudge_through_the_cli_on_a_hot_verb(repo_declaring):
    repo_declaring(recommended_foldyard_version="99.0.0")
    assert "99.0.0" not in _invoke(["ps", "--help"]).output


def test_floor_blocks_through_the_cli(repo_declaring):
    repo_declaring(min_foldyard_version="99.0.0")
    result = _invoke(["ps", "--help"])
    assert result.exit_code == 1
    assert "99.0.0" in result.output


def test_gate_still_runs_when_the_worktree_pin_short_circuits(repo_declaring, monkeypatch):
    # in_box / an explicit WORKTREE skip the pin. The gate must not be skipped with it — the
    # box is where a foldyard mismatched to the repo does the most damage.
    repo_declaring(min_foldyard_version="99.0.0")
    monkeypatch.setenv("WORKTREE", "some-worktree")
    assert _invoke(["ps", "--help"]).exit_code == 1
