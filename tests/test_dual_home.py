"""foldyard's OWN foldyard.toml must be dual-home: it has to work both as a monorepo subdir
and as a standalone repo root (ADR-0013). That holds iff it references nothing OUTSIDE its
own directory — no `../` escapes, no absolute paths — so the file travels verbatim on
extraction. This test is the guard."""

from __future__ import annotations

import tomllib
from pathlib import Path

# tests/ sits directly under the foldyard project root.
FOLDYARD_DIR = Path(__file__).resolve().parents[1]
OWN_TOML = FOLDYARD_DIR / "foldyard.toml"


def _strings(value) -> list[str]:
    """Every string leaf in a parsed-TOML structure."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def test_own_foldyard_toml_exists_and_parses():
    assert OWN_TOML.is_file(), "foldyard should ship its own foldyard.toml (its dev box)"
    tomllib.loads(OWN_TOML.read_text())


def test_own_config_references_nothing_outside_its_dir():
    doc = tomllib.loads(OWN_TOML.read_text())
    for s in _strings(doc):
        assert ".." not in s, f"path escapes foldyard/: {s!r}"
        assert not s.startswith("/"), f"absolute path won't travel to a standalone repo: {s!r}"


def test_own_config_declares_no_compose_or_box_image():
    # foldyard has no services to colocate and dogfoods the packaged generic box image.
    # `[[box.tools]]` is fine — it layers installs ON that image; a `[box].image` would
    # replace it, and a Dockerfile path is exactly the kind of outside-the-dir reference
    # this file must not carry.
    doc = tomllib.loads(OWN_TOML.read_text())
    assert "compose" not in doc.get("project", {})
    assert "image" not in doc.get("box", {})
