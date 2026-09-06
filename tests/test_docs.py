"""Guards docs/configuration.md against bitrot: every table and key the ``foldyard init``
template teaches (active OR commented) must be documented in the config reference, and the
reference's own table headings must all be real config surfaces. This is deliberately
one-directional — the reference may document more than the starter template shows — but a
template that grows a key the reference doesn't explain fails here."""

from __future__ import annotations

import re
from pathlib import Path

from foldyard import init

DOCS = Path(__file__).resolve().parents[1] / "docs" / "configuration.md"

# Template lines that look like keys but aren't config: prose, prompts, shell.
_NON_KEYS = {"You", "with"}


def _template_surfaces() -> tuple[set[str], set[str]]:
    """(tables, keys) mentioned by the init template, uncommented or commented."""
    body = init.render(init.InitOptions(name="probe"))
    tables: set[str] = set()
    keys: set[str] = set()
    in_multiline = False
    for line in body.splitlines():
        stripped = line.lstrip("# ").rstrip()
        if in_multiline:
            if stripped.endswith('"""'):
                in_multiline = False
            continue
        if m := re.fullmatch(r"\[\[?([a-z0-9.\-]+)\]\]?", stripped):
            tables.add(m.group(1))
            continue
        if m := re.match(r"([a-zA-Z_]+) = ", stripped):
            # ALL-CAPS keys are the consumer's own [ports] env-var names, not config keys.
            if m.group(1) not in _NON_KEYS and not m.group(1).isupper():
                keys.add(m.group(1))
            if stripped.endswith('"""'):
                in_multiline = True
    return tables, keys


def test_init_template_is_documented_in_configuration_md():
    doc = DOCS.read_text()
    tables, keys = _template_surfaces()
    assert tables and keys  # the parser found the template's surfaces at all
    missing_tables = {t for t in tables if f"[{t}]" not in doc}
    missing_keys = {k for k in keys if f"`{k}`" not in doc and f"{k} = " not in doc}
    assert not missing_tables, (
        f"init template tables absent from configuration.md: {missing_tables}"
    )
    assert not missing_keys, f"init template keys absent from configuration.md: {missing_keys}"


def test_configuration_md_headings_are_real_surfaces():
    # Every `## [table]`-style heading in the reference must be a table config.py reads —
    # the reference must never document config that doesn't exist.
    known = {
        "project",
        "machine",
        "engine",
        "ports",
        "proxy",
        "inject",
        "secret",
        "overlay",
        "require",
        "box",
        "claude",
        "codex",
        "vscode",
        "plugins.gcp-metadata",
        "plugins.github",
        "plugins.auth0-sim",
        "plugins.llm",
        "box.tools",
    }
    doc = DOCS.read_text()
    documented = set(re.findall(r"^#+ `?\[\[?([a-z0-9.\-]+)\]\]?", doc, flags=re.M))
    unknown = documented - known - {"plugins.<name>"}
    assert not unknown, f"configuration.md documents unknown tables: {unknown}"
