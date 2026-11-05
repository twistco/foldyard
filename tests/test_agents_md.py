"""AGENTS.md is the one file every agent loads, and nothing fails when it lies.

It's a map, so these hold it to what it points at. A wrong step here costs every session, and a pointer
that's dead is a rule nobody reads. The docs build checks links inside docs/, but not from this file,
which is outside it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
AGENTS = ROOT / "AGENTS.md"
DEV_PAGES = sorted((ROOT / "docs" / "dev").glob("*.md"))
LINK = re.compile(r"\]\((?!https?://)([^)#\s]+)(#[^)]*)?\)")
MAX_LINES = 200  # https://code.claude.com/docs/en/memory: "target under 200 lines"


def _text() -> str:
    return AGENTS.read_text()


@pytest.mark.parametrize("name", ["CLAUDE.md", "CLAUDE.local.md", "DEVELOPMENT.md"])
def test_no_file_takes_its_place(name):
    """Claude Code reads AGENTS.md only when no CLAUDE.md sits here or above. A worktree lives under
    the main checkout, so one committed CLAUDE.md would silently hide AGENTS.md from every worktree.
    DEVELOPMENT.md is the guide this replaced: a second copy is how the first one went stale."""
    assert not (ROOT / name).exists()


def test_it_stays_a_map():
    assert len(_text().splitlines()) < MAX_LINES


def test_every_link_lands_on_a_file():
    links = [m.group(1) for m in LINK.finditer(_text())]
    assert links
    assert [link for link in links if not (ROOT / link).exists()] == []


def test_every_guide_page_is_pointed_at():
    assert DEV_PAGES
    links = {m.group(1) for m in LINK.finditer(_text())}
    assert [p.name for p in DEV_PAGES if f"docs/dev/{p.name}" not in links] == []


def test_every_guide_page_is_on_the_docs_site():
    nav = (ROOT / "mkdocs.yml").read_text()
    assert [p.name for p in DEV_PAGES if f"- dev/{p.name}\n" not in nav] == []


def test_every_recipe_it_names_exists():
    recipes = set(
        re.findall(r"(?m)^([a-z][a-z0-9-]*)(?: [^:\n]*)?:", (ROOT / "justfile").read_text())
    )
    named = set(re.findall(r"\bjust ([a-z][a-z0-9-]*)", _text()))
    assert named
    assert sorted(named - recipes) == []


def _mapped() -> set[str]:
    where = _text().split("## Where things are")[1]
    return set(re.findall(r"`([a-z_]+)`", where))


def test_the_map_holds_only_real_modules():
    modules = {p.stem for p in (ROOT / "src" / "foldyard").glob("*.py")} | {"plugins"}
    assert sorted(_mapped() - modules) == []


def test_every_module_is_on_the_map():
    modules = {p.stem for p in (ROOT / "src" / "foldyard").glob("*.py")} - {"__init__", "__main__"}
    assert sorted(modules - _mapped()) == []


def test_the_host_tier_ask_is_there():
    """A box can't run the host tier, so asking for the run is part of the work, and it has to be in
    what's always loaded for an agent to know that."""
    text = _text()
    assert "just nested-test --ref <hash>" in text
    assert "[run-e2e]" in text
