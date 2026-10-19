#!/usr/bin/env python3
"""``fy changelog`` — what changed between the foldyard a project declares and the one running.

A project's ``foldyard.toml`` names the foldyard it was written against
(``[project].min_foldyard_version`` / ``recommended_foldyard_version``, :mod:`foldyard.compat`),
and an install can be any number of releases past that. Catching the config up means reading
every release in between — and the whole ``CHANGELOG.md`` is hundreds of lines, most of it
history the project already absorbed. This prints just the slice: the releases after the
declared version, up to and including the installed one, oldest first, so they read in the
order they happened.

Served from the INSTALLED package for the same reason as ``fy docs``: version-matched by
construction and readable with no egress (a box under ``default_deny`` can't fetch a release
page). The changelog is force-included into the wheel; running from a source checkout reads the
repo's own copy. Only RELEASED sections are in it — pending ``.changes/`` fragments are not part
of any version a consumer can install.

The loop this closes: upgrade, ``fy changelog``, apply what it says to ``foldyard.toml``, raise
``min_foldyard_version`` in the same commit — which moves the next ``fy changelog``'s starting
point past what was just absorbed. Stdlib only.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .compat import _parse

_RELEASE = re.compile(r"^## (?P<version>\S+)(?: — (?P<date>.+))?$", re.MULTILINE)
_SECTION = re.compile(r"^### (?P<name>.+)$", re.MULTILINE)

#: The sections that can ask the reader to DO something after upgrading — edit config, rename a
#: secret, recreate a VM. ``--action`` keeps these plus each release's summary prose (which is
#: where a release says "every VM needs one restart"). Added/Fixed are left out: worth knowing,
#: never a step to take.
ACTION_SECTIONS = ("Security", "Removed", "Deprecated", "Changed")


@dataclass(frozen=True)
class Release:
    version: str
    date: str
    body: str  # everything under the `## ` heading, up to the next one, stripped

    @property
    def heading(self) -> str:
        return f"## {self.version}" + (f" — {self.date}" if self.date else "")


def changelog_path() -> Path | None:
    """The packaged copy, else the repo's own when running from source; ``None`` when neither
    exists (a wheel built without the force-include — degrade to a pointer, not a traceback)."""
    packaged = Path(__file__).resolve().parent / "assets" / "CHANGELOG.md"
    if packaged.is_file():
        return packaged
    # …/foldyard/src/foldyard/changelog.py → parents[2] = the project root
    source = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
    return source if source.is_file() else None


def releases(text: str) -> list[Release]:
    """Every ``## X.Y.Z — date`` section, in file order (newest first). A heading whose version
    won't parse is skipped alone — it can't be placed in a range."""
    heads = list(_RELEASE.finditer(text))
    out = []
    for i, m in enumerate(heads):
        if _parse(m.group("version")) is None:
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append(Release(m.group("version"), m.group("date") or "", text[m.end() : end].strip()))
    return out


def between(rels: list[Release], since: str, until: str | None) -> list[Release]:
    """The releases in ``(since, until]``, oldest first — half-open on the left because you
    already have ``since``'s changes, closed on the right because ``until`` is what you run.
    ``until=None`` (an installed version that can't be ordered — a local dev build) means no
    upper bound."""
    low, high = _parse(since), _parse(until)
    if low is None:
        return []
    picked = []
    for rel in rels:
        at = _parse(rel.version)
        if at is not None and low < at and (high is None or at <= high):
            picked.append((at, rel))
    return [rel for _, rel in sorted(picked, key=lambda pair: pair[0])]


def action_only(body: str) -> str:
    """``body`` cut down to its summary prose and the :data:`ACTION_SECTIONS`."""
    heads = list(_SECTION.finditer(body))
    keep = [body[: heads[0].start()].strip() if heads else body.strip()]
    for i, m in enumerate(heads):
        if m.group("name").strip() in ACTION_SECTIONS:
            end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
            keep.append(body[m.start() : end].strip())
    return "\n\n".join(part for part in keep if part)


def declared() -> tuple[str, str] | None:
    """``(version, key)`` — the version this project's config was written against: the higher of
    its two declarations, since a recommendation above the floor is what the repo was last moved
    to. ``None`` when it declares neither (or nothing parseable)."""
    from . import config

    found: list[tuple[tuple[int, ...], str, str]] = []
    for value, key in (
        (config.min_foldyard_version(), "min_foldyard_version"),
        (config.recommended_foldyard_version(), "recommended_foldyard_version"),
    ):
        parsed = _parse(value)
        if value is not None and parsed is not None:
            found.append((parsed, value, key))
    if not found:
        return None
    _, value, key = max(found, key=lambda row: row[0])
    return value, key


def newer_than(version: str, installed: str) -> int:
    """How many released versions lie in ``(version, installed]`` — 0 when the changelog isn't
    available. For the doctor row's pointer; never raises."""
    path = changelog_path()
    if path is None:
        return 0
    try:
        return len(between(releases(path.read_text()), version, installed))
    except OSError:
        return 0


def main(since: str | None = None, until: str | None = None, *, action: bool = False) -> int:
    from . import __version__

    path = changelog_path()
    if path is None:
        print(
            "✗ this install has no changelog (a wheel built without it). Releases: "
            "https://github.com/twistco/foldyard/releases",
            file=sys.stderr,
        )
        return 1

    source = "--since"
    if since is None:
        found = declared()
        if found is None:
            print(
                "✗ no starting version: no foldyard.toml here declares `min_foldyard_version`.\n"
                "  Pass one: `fy changelog --since 0.3.0`.",
                file=sys.stderr,
            )
            return 2
        since, key = found
        source = f"[project].{key}"
    if _parse(since) is None:
        print(f"✗ can't order {since!r} as a version (expected e.g. 0.3.1).", file=sys.stderr)
        return 2

    upper = until if until is not None else __version__
    if until is not None and _parse(until) is None:
        print(f"✗ can't order {until!r} as a version (expected e.g. 0.4.0).", file=sys.stderr)
        return 2
    # An installed version that can't be ordered (a local dev build) bounds nothing — show
    # everything after `since` rather than nothing.
    bound = upper if _parse(upper) is not None else None

    picked = between(releases(path.read_text()), since, bound)
    span = f"{since} → {upper}"
    if not picked:
        low, high = _parse(since), _parse(bound)
        if low is not None and high is not None and low >= high:
            ahead = " — the project is ahead of this install; upgrade fy" if low > high else ""
            print(f"Nothing to read: {span} ({since} from {source}){ahead}.")
        else:
            print(f"No releases in {span} ({since} from {source}).")
        return 0

    filt = " — Summary, Security, Removed, Deprecated, Changed only" if action else ""
    print(
        f"# foldyard changes {span}: {len(picked)} release(s), oldest first\n"
        f"# starting point: {since}, from {source}{filt}\n"
    )
    for rel in picked:
        body = action_only(rel.body) if action else rel.body
        print(f"{rel.heading}\n\n{body or '(nothing in these sections)'}\n")
    if source != "--since" and bound is not None:
        print(
            f"# Once foldyard.toml is caught up, raise {source.split('.')[-1]} to {upper} in the "
            "same commit — the next `fy changelog` then starts there."
        )
    return 0
