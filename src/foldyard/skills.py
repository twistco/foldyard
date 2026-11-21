"""``foldyard skill list|install`` — Claude Code skills bundled in the package.

foldyard ships skills under ``src/foldyard/assets/skills/<name>/`` so they version with the
tool and need no separate registry. ``install`` copies one into a consumer repo's
``.claude/skills/`` where Claude Code discovers it. Stdlib only.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import config, mountwrite


def _bundled_dir() -> Path:
    return Path(__file__).resolve().parent / "assets" / "skills"


def bundled() -> list[str]:
    """Names of the skills shipped in the package (dirs containing a SKILL.md)."""
    root = _bundled_dir()
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if (d / "SKILL.md").is_file())


def _describe(name: str) -> str:
    """First non-empty line of a skill's `description:` frontmatter (best-effort, one line)."""
    try:
        text = (_bundled_dir() / name / "SKILL.md").read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return ""


def list_() -> int:
    names = bundled()
    if not names:
        print("(no bundled skills)")
        return 0
    print("Bundled foldyard skills (install: foldyard skill install <name>):")
    for name in names:
        desc = _describe(name)
        # keep the listing to one tidy line per skill
        summary = (desc[:88] + "…") if len(desc) > 89 else desc
        print(f"  • {name}" + (f" — {summary}" if summary else ""))
    return 0


def write_bundled(root: Path, rel: Path, name: str) -> None:
    """Write bundled skill ``name``'s files under ``root/rel``, following no link below ``root``
    (:mod:`foldyard.mountwrite`): the checkout is box-writable, so a ``.claude`` there may be a
    link to a directory of yours. Files already there are overwritten, never deleted first.
    Raises ``OSError`` when a directory on the way is a link."""
    src = _bundled_dir() / name
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        mountwrite.write(root, rel / f.relative_to(src), f.read_bytes())


def install(name: str, *, force: bool = False) -> int:
    """Copy a bundled skill into ``<repo>/.claude/skills/<name>/`` (:func:`write_bundled`)."""
    names = bundled()
    if name not in names:
        avail = ", ".join(names) or "(none)"
        print(f"✗ no bundled skill '{name}'. Available: {avail}")
        return 1
    root = config.repo_root()
    rel = Path(".claude") / "skills" / name
    dest = root / rel
    if os.path.lexists(dest) and not force:
        print(f"✗ {dest} already exists — pass --force to overwrite.")
        return 1
    try:
        write_bundled(root, rel, name)
    except OSError as e:
        print(f"✗ could not install skill (a link in the way?): {e}")
        return 1
    print(f"✓ installed skill '{name}' → {dest}")
    print("  Claude Code picks it up from .claude/skills/; invoke it with /" + name + ".")
    return 0
