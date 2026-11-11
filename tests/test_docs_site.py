"""The docs site's build hook (docs-site/hooks.py), in the fast suite: the docs workflow builds the
site `--strict` only when docs change, and the two things here would fail it — or worse, pass it
wrong — in ways that are easier to read as a unit than as a dead link in a build log."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("docs_site_hooks", ROOT / "docs-site" / "hooks.py")
assert _spec and _spec.loader
hooks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hooks)


@pytest.mark.parametrize(
    ("heading", "github_id"),
    [
        # An em dash dropped, both spaces kept: the shape most of the manual's anchors have.
        (
            "Degraded — the switch is on but the credential stopped working",
            "degraded--the-switch-is-on-but-the-credential-stopped-working",
        ),
        # toc hands over plain text; GitHub keeps the `name` in `[plugins.<name>]`.
        ("[plugins.<name>]", "pluginsname"),
        (
            "The 2026-07 re-sweep — the placeholder wave, and what's still open",
            "the-2026-07-re-sweep--the-placeholder-wave-and-whats-still-open",
        ),
        ("Step 3 — bring your stack in", "step-3--bring-your-stack-in"),
    ],
)
def test_heading_ids_are_githubs(heading, github_id):
    """The pages link anchors as GitHub derives them, where they are read today."""
    assert hooks.github_slug(heading) == github_id


@pytest.mark.parametrize(
    ("markdown", "target"),
    [
        ("[x](./a.md#b)", "./a.md#b"),
        # Alt text that wraps and names TOML tables, as the diagrams' alt texts do.
        ("![one [[inject]] row\nbecomes a switch](https://x/a.svg)", "https://x/a.svg"),
        ("see [`[machine] firewall`](../c.md)", "../c.md"),
        # `<…>`: how mdformat writes a target holding parentheses — read whole, not cut at `)`.
        ("[`V`](<https://x/init(url:readonly:)>)", "<https://x/init(url:readonly:)>"),
        ("[a](<./b c.md>)", "<./b c.md>"),
    ],
)
def test_the_link_pattern_reads_the_manuals_links(markdown, target):
    assert [m.group(2) for m in hooks._LINK.finditer(markdown)] == [target]
