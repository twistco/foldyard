"""Everything the package imports at run time comes from its own `[project].dependencies`.

The proxy's 401 re-issue imported `requests`, which only the opt-in e2e group installed: the unit
tests stub it and the live test runs with that group, so every real install raised
ModuleNotFoundError there instead of re-issuing (found in a supervisor log, 2026-11-24)."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "foldyard"
# Import name → the declared dependency that brings it, where the two differ. `yaml` and `dotenv`
# are podman-compose's own requirements, `rich` Textual's.
PROVIDED_BY = {
    "dotenv": "podman-compose",
    "jwt": "pyjwt",
    "podman_compose": "podman-compose",
    "rich": "textual",
    "yaml": "podman-compose",
}


def _imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def _declared() -> set[str]:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    return {re.split(r"[\[<>=!~; ]", d, maxsplit=1)[0].lower() for d in deps}


def test_every_import_is_the_stdlibs_or_a_declared_dependencys():
    declared = _declared()
    siblings = {p.stem for p in PACKAGE.rglob("*.py")}  # an asset importing the one beside it
    missing = {
        f"{path.relative_to(ROOT)}: {name}"
        for path in PACKAGE.rglob("*.py")
        for name in _imports(path) - set(sys.stdlib_module_names) - siblings - {"foldyard"}
        if PROVIDED_BY.get(name, name) not in declared
    }
    assert not missing
