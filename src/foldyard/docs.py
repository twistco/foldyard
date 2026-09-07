#!/usr/bin/env python3
"""``fy docs [topic]`` — the reference manual, served from the INSTALLED package.

An agent (or a human) in the box needs depth beyond ``--help``: what a posture rung actually
grants, why capture and the allowlist are different controls, what the box structurally cannot do.
Three ways to deliver that, and the other two are worse:

  - **Read the docs in the checkout** — only works for a consumer that vendors foldyard (the
    pre-spinout carve-out, ADR-0013), which is exactly the arrangement we are ending.
  - **Clone the open-source repo** — gives you ``main``, not the version installed here. That is
    the "the thing you're reading isn't the thing that's running" failure ADR-0022 exists to stop;
    reintroducing it for the documentation would be perverse. It also needs an egress grant under
    ``default_deny`` and costs thousands of lines of context to answer one question.
  - **Ship them in the wheel** (this) — version-matched by construction, works offline, and paged
    one topic at a time.

The consumer-facing subset is force-included at build time (see ``pyproject.toml``); contributor
material (DEVELOPMENT.md, the design notes, the wall kit) deliberately stays in the repo. When
foldyard is running from a source checkout — an editable install, or the repo itself — the repo's
``docs/`` is used instead, so a contributor sees their edits without rebuilding.

Reading is what this is for; CONTRIBUTING is the co-dev mount (ADR-0020), which gives you the
source you're actually running rather than a drifted copy. Stdlib only.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The public repo. Humans browse it and
# contributors clone it; an AGENT should reach for `fy docs` instead — see the module docstring.
REPO_URL = "https://github.com/twistco/foldyard"


def docs_dir() -> Path | None:
    """Where the docs are: the packaged copy, else the repo's ``docs/`` when running from source
    (editable install / the checkout itself). ``None`` when neither exists — a wheel built without
    the force-include, which must degrade to a pointer rather than a traceback."""
    packaged = Path(__file__).resolve().parent / "assets" / "docs"
    if packaged.is_dir():
        return packaged
    # …/foldyard/src/foldyard/docs.py → parents[2] = the project root
    source = Path(__file__).resolve().parents[2] / "docs"
    return source if source.is_dir() else None


def _topics(root: Path) -> dict[str, Path]:
    """topic name → file, for every shipped markdown page (ADRs keyed as ``adr-0022``)."""
    out: dict[str, Path] = {}
    for path in sorted(root.glob("*.md")):
        if path.stem != "README":  # a repo-navigation index; `fy docs` IS that index here
            out[path.stem] = path
    for path in sorted((root / "adrs").glob("[0-9]*.md")):
        out[f"adr-{path.name.split('-')[0]}"] = path
    return out


def _blurb(path: Path) -> str:
    """A one-line description: the first non-heading, non-empty line of the page, trimmed."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines[1:]:
        text = line.strip()
        if text and not text.startswith(("#", "-", "|", ">", "!", "*")):
            return text[:96].rstrip(" .,") + ("…" if len(text) > 96 else "")
    return ""


def _adr_title(path: Path) -> str:
    """An ADR's own title line, minus the `# ADR-NNNN — ` prefix (they're self-describing)."""
    try:
        first = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return ""
    return first.lstrip("# ").split("—", 1)[-1].strip()


def _resolve(topics: dict[str, Path], want: str) -> Path | None:
    """Exact name, else a unique prefix/substring match — so ``fy docs network`` finds
    ``networking`` and ``fy docs 0022`` finds the ADR. Ambiguity resolves to nothing (the caller
    lists the candidates) rather than to a guess."""
    want = want.strip().lower().removesuffix(".md")
    if want in topics:
        return topics[want]
    matches = [name for name in topics if want in name]
    return topics[matches[0]] if len(matches) == 1 else None


def list_() -> int:
    root = docs_dir()
    if root is None:
        print(f"✗ no docs in this install — read them at {REPO_URL}", file=sys.stderr)
        return 1
    topics = _topics(root)
    guides = {name: path for name, path in topics.items() if not name.startswith("adr-")}
    print("foldyard docs — `fy docs <topic>` (this install's own version, no network needed)\n")
    width = max((len(n) for n in guides), default=0)
    for name, path in guides.items():
        print(f"  {name:<{width}}  {_blurb(path)}")
    adrs = {n: p for n, p in topics.items() if n.startswith("adr-")}
    if adrs:
        print(f"\n  Decisions ({len(adrs)}) — the WHY, one per file: `fy docs adr-0001` …")
        for name, path in adrs.items():
            print(f"    {name}  {_adr_title(path)}")
    print(f"\n  Source, issues, contributing: {REPO_URL}")
    return 0


def show(topic: str) -> int:
    root = docs_dir()
    if root is None:
        print(f"✗ no docs in this install — read them at {REPO_URL}", file=sys.stderr)
        return 1
    topics = _topics(root)
    path = _resolve(topics, topic)
    if path is None:
        print(f"✗ no doc topic matching {topic!r}.", file=sys.stderr)
        print(f"  Available: {', '.join(n for n in topics if not n.startswith('adr-'))}")
        print("  Plus the ADRs — `fy docs` lists them.")
        return 1
    print(path.read_text(encoding="utf-8", errors="replace"))
    return 0


def main(topic: str = "") -> int:
    return show(topic) if topic else list_()
