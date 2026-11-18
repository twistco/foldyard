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


def test_inline_code_has_no_background(monkeypatch):
    """rich styles inline `code` cyan on BLACK, a black slab on a light terminal theme; foldyard
    keeps the cyan and drops the background on every help screen."""
    import typer.rich_utils

    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", True)
    monkeypatch.setattr(typer.rich_utils, "COLOR_SYSTEM", "standard")
    for argv in (["--help"], ["box", "--help"], ["claude", "--help"]):
        out = CliRunner().invoke(cli.app, argv).output
        assert "\x1b[1;36m" in out, argv  # code is still styled…
        assert ";40m" not in out and "[40m" not in out, argv  # …on no background


def test_every_listed_summary_is_one_capitalised_line():
    """A verb's line in a listing is its help's first paragraph: one short sentence, capitalised —
    the detail belongs in `fy <verb> --help`."""
    import inspect

    def walk(cmd, path):
        for name, sub in getattr(cmd, "commands", {}).items():
            if sub.hidden:
                continue
            first = inspect.cleandoc(sub.help or "").split("\n\n")[0].replace("\n", " ")
            yield " ".join([*path, name]), first
            yield from walk(sub, [*path, name])

    for verb, first in walk(typer.main.get_command(cli.app), ["fy"]):
        assert first.lstrip('"(')[:1].isupper(), (verb, first)
        assert len(first) <= 95, (verb, len(first), first)
