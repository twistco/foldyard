"""`fy --help` grouped into panels (cli.panels): every visible verb in exactly one, the experimental
features' verbs (experimental.REGISTER's `verbs`) in their own panel, last."""

from __future__ import annotations

import typer
from typer.core import TyperGroup
from typer.testing import CliRunner

from foldyard import cli, experimental


def _visible() -> set[str]:
    group = typer.main.get_command(cli.app)
    assert isinstance(group, TyperGroup)
    return {name for name, cmd in group.commands.items() if not cmd.hidden}


def test_every_visible_verb_is_in_exactly_one_panel():
    listed = [v for _, verbs in cli.panels() for v in verbs]
    assert sorted(listed) == sorted(set(listed)), "a verb listed twice"
    assert set(listed) == _visible(), (
        f"unpanelled: {_visible() - set(listed)}; not a visible verb: {set(listed) - _visible()}"
    )


def test_the_experimental_panel_is_the_registers_verbs_and_comes_last():
    title, verbs = cli.panels()[-1]
    assert title == cli.EXPERIMENTAL
    assert set(verbs) == {v for f in experimental.REGISTER for v in f.verbs} != set()
    for _, others in cli.panels()[:-1]:
        assert not set(others) & set(verbs)


def test_help_prints_the_panels_in_order():
    out = CliRunner().invoke(cli.app, ["--help"]).output
    positions = [out.find(title) for title, _ in cli.panels()]
    assert -1 not in positions and positions == sorted(positions)
    assert "Commands" not in out  # nothing left in typer's default panel
