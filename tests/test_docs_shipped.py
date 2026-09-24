"""The docs the wheel ships (`fy docs`) must stand on their own, and speak to every host.

Two guards over pyproject's force-include set — the pages a consumer's install serves offline:

- a relative link from a shipped page must land on another shipped page. Running from a checkout
  hides a broken one (the repo file is right there); a consumer's `fy docs` has only the wheel, so
  a link to the README, the design notes or docs/archive/ must be a GitHub URL instead;
- the manual says "your computer", not "Mac": foldyard runs on Linux and WSL2 hosts too, and a page
  that says "on the Mac" tells those readers the instruction isn't for them. "macOS" stays allowed
  for facts that really are macOS-only (brew, the keychain, notifications).

The bundled skills and `fy --help` are held to the second rule as well: they're the same audience.
ADRs are exempt from it — they're dated records, and a finding measured on a Mac stays one.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foldyard import cli

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "src" / "foldyard" / "assets" / "skills"
_LINK = re.compile(r"\]\((?!https?:|mailto:)([^)\s#]*)(?:#[^)\s]*)?\)")
_MAC = re.compile(r"\bMacs?\b")


def _shipped() -> list[Path]:
    build = tomllib.loads((ROOT / "pyproject.toml").read_text())
    pages: list[Path] = []
    for src in build["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]:
        path = ROOT / src
        pages += sorted(path.glob("*.md")) if path.is_dir() else [path]
    return pages


SHIPPED = _shipped()
MANUAL = [p for p in SHIPPED if p.parent.name != "adrs"]


def _ids(pages: list[Path]) -> list[str]:
    return [str(p.relative_to(ROOT)) for p in pages]


@pytest.mark.parametrize("page", SHIPPED, ids=_ids(SHIPPED))
def test_a_shipped_page_links_only_to_shipped_pages(page: Path):
    shipped = {p.resolve() for p in SHIPPED}
    broken = [
        target
        for target in _LINK.findall(page.read_text())
        if target and (page.parent / target).resolve() not in shipped
    ]
    assert not broken, (
        f"{page.name} links to pages `fy docs` doesn't ship: {broken} — "
        "use https://github.com/twistco/foldyard/blob/main/<path>"
    )


@pytest.mark.parametrize(
    "page",
    MANUAL + sorted(SKILLS.rglob("*.md")),
    ids=_ids(MANUAL + sorted(SKILLS.rglob("*.md"))),
)
def test_the_manual_and_skills_say_your_computer_not_mac(page: Path):
    hits = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(page.read_text().splitlines(), 1)
        if _MAC.search(line)
    ]
    assert not hits, "say 'your computer' (or 'macOS' for a macOS-only fact):\n" + "\n".join(hits)


def _help_pages() -> list[list[str]]:
    """argv for every command's `--help`, walked from the typer app."""

    def name(command) -> str:
        return command.name or command.callback.__name__.replace("_", "-")

    out: list[list[str]] = [[]]
    out += [[name(c)] for c in cli.app.registered_commands if not c.hidden]
    for group in cli.app.registered_groups:
        sub = group.typer_instance
        assert sub is not None and isinstance(sub.info.name, str)
        out.append([sub.info.name])
        out += [[sub.info.name, name(c)] for c in sub.registered_commands if not c.hidden]
    return out


def test_cli_help_says_your_computer_not_mac():
    runner = CliRunner()
    hits = []
    pages = _help_pages()
    assert len(pages) > 40  # the walk found the verbs at all
    for argv in pages:
        text = runner.invoke(cli.app, [*argv, "--help"]).output
        hits += [f"fy {' '.join(argv)}: {m.group(0)}" for m in _MAC.finditer(text)]
    assert not hits, hits
