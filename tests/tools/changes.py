"""Changelog fragments: one ``.changes/<slug>.md`` per change, folded into CHANGELOG.md at release.

A shared ``## Unreleased`` section made every pair of open PRs conflict over the same lines, so
each change carries its entry as its own file (format: ``.changes/README.md``) and ``just release``
runs ``prepare`` to roll them into a version section. Stdlib-only: the recipe runs it with
``uv run --no-project`` so preparing a release never touches the project env or ``uv.lock``, and
the release workflow runs ``notes`` with the runner's own python.

    python tests/tools/changes.py check              # validate .changes/ (also a test in `check`)
    python tests/tools/changes.py prepare <X.Y.Z|patch|minor|major> <YYYY-MM-DD>
    python tests/tools/changes.py notes <X.Y.Z>      # one release's section, for the GitHub Release
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Most consequential first: what a consumer must act on, then what may break them, then the rest.
# `Summary` is the exception: prose under no heading, above them all (0.3.2's "upgrade now").
SUMMARY = "Summary"
SECTIONS = (SUMMARY, "Security", "Removed", "Deprecated", "Changed", "Added", "Fixed")
BUMPS = ("patch", "minor", "major")
CHANGES = Path(".changes")
# Each identifier is 0 or starts 1-9: `01.2.3` is not a semantic version, and PyPI would normalise
# it to 1.2.3 — a number that then no longer matches the tag or pyproject.
SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
# What a release commit carries: the three version-bearing files, the consumed fragments, and our
# own foldyard.toml's reasons entry (written by hand before running the release).
RELEASE_PATHS = ("pyproject.toml", "uv.lock", "CHANGELOG.md", ".changes", "foldyard.toml")


class FragmentError(ValueError):
    """A refusal, worded for the person running the release."""


@dataclass(frozen=True)
class Fragment:
    path: Path
    section: str
    bump: str
    body: str


def parse(path: Path) -> Fragment:
    text = path.read_text()
    m = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if not m:
        raise FragmentError(f"{path.name}: no `---` frontmatter block (see .changes/README.md)")
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if not sep or key.strip() not in ("section", "bump"):
            raise FragmentError(f"{path.name}: unknown frontmatter line {line!r} (section, bump)")
        meta[key.strip()] = value.strip()
    section, bump, body = meta.get("section", ""), meta.get("bump", ""), m.group(2).strip()
    if section not in SECTIONS:
        raise FragmentError(f"{path.name}: section {section!r} is not one of {', '.join(SECTIONS)}")
    if bump not in BUMPS:
        raise FragmentError(f"{path.name}: bump {bump!r} is not one of {', '.join(BUMPS)}")
    if not body:
        raise FragmentError(f"{path.name}: the entry is empty")
    if section != SUMMARY and not body.startswith("- "):
        raise FragmentError(f"{path.name}: the entry must be a `- ` bullet (or several)")
    return Fragment(path, section, bump, body)


def load(directory: Path = CHANGES) -> list[Fragment]:
    return [parse(p) for p in sorted(directory.glob("*.md")) if p.name != "README.md"]


def _parts(version: str) -> tuple[int, int, int]:
    m = SEMVER.fullmatch(version)
    if not m:
        raise FragmentError(f"'{version}' is not X.Y.Z (or patch, minor, major)")
    major, minor, patch = (int(g) for g in m.groups())
    return major, minor, patch


def next_version(current: str, keyword: str) -> str:
    major, minor, patch = _parts(current)
    return {
        "patch": f"{major}.{minor}.{patch + 1}",
        "minor": f"{major}.{minor + 1}.0",
        "major": f"{major + 1}.0.0",
    }[keyword]


def bump_level(current: str, new: str) -> str | None:
    """Which part ``new`` steps up from ``current`` — ``None`` when it is no step up at all."""
    old, nxt = _parts(current), _parts(new)
    if nxt <= old:
        return None
    return BUMPS[2 - next(i for i in range(3) if nxt[i] != old[i])]


def required_bump(fragments: list[Fragment]) -> str:
    return max((f.bump for f in fragments), key=BUMPS.index, default="patch")


def render(version: str, date: str, fragments: list[Fragment]) -> str:
    out = [f"## {version} — {date}\n"]
    for section in SECTIONS:
        bodies = [f.body for f in fragments if f.section == section]
        if bodies and section == SUMMARY:
            out.append("\n" + "\n\n".join(bodies) + "\n")
        elif bodies:
            out.append(f"\n### {section}\n\n" + "\n".join(bodies) + "\n")
    return "".join(out)


def _release_heading(version: str) -> re.Pattern[str]:
    return re.compile(rf"^## {re.escape(version)} — .*$", re.M)


def roll(changelog: str, section: str) -> str:
    """``section`` above the newest release: after the header, wherever the first ``## `` is."""
    first = re.search(r"^## ", changelog, re.M)
    at = first.start() if first else len(changelog)
    return changelog[:at] + section + "\n" + changelog[at:]


def notes(changelog: str, version: str) -> str:
    """One release's body — its heading dropped, up to the next release."""
    m = _release_heading(version).search(changelog)
    if not m:
        raise FragmentError(f"CHANGELOG.md has no '## {version}' section")
    nxt = re.search(r"^## ", changelog[m.end() :], re.M)
    end = m.end() + nxt.start() if nxt else len(changelog)
    return changelog[m.end() : end].strip() + "\n"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _in_merge_order(fragments: list[Fragment]) -> list[Fragment]:
    """Oldest-landed first, so a section reads in merge order: git's own commit order of the
    commits that ADDED each file (not their timestamps — one-second resolution ties). Files one
    commit added keep git's name order; a fragment not yet committed sorts last. NUL-separated:
    a name is data, so neither whitespace nor git's quoting may reshape it. A name added again
    after a delete keeps its LATEST landing — the one the current file came from."""
    log = _git(
        "log", "--reverse", "--diff-filter=A", "--name-only", "-z", "--format=", "--", str(CHANGES)
    )
    landed = {Path(name).name: i for i, name in enumerate(log.stdout.split("\0")) if name}
    return sorted(fragments, key=lambda f: (landed.get(f.path.name, len(landed)), f.path.name))


def _dirty(allowed: tuple[str, ...]) -> list[str]:
    status = _git("status", "--porcelain", "--untracked-files=all").stdout.splitlines()
    paths = [line[3:].split(" -> ")[-1] for line in status]
    return [p for p in paths if not any(p == a or p.startswith(a + "/") for a in allowed)]


def _stanza(version: str) -> str:
    # Only the `foldyard` package's own stanza; every dependency has a version line too.
    return f'name = "foldyard"\nversion = "{version}"'


def prepare(target: str, date: str) -> str:
    """Bump pyproject.toml + uv.lock, fold ``.changes/`` into CHANGELOG.md, delete the folded
    fragments — or recognise a release prepared earlier and not yet tagged, and change nothing.
    Validates EVERYTHING before writing anything: a half-bumped tree is worse than a refusal.
    Returns the version; ``just release`` then checks, builds, commits and tags it."""
    pyproject, lock = Path("pyproject.toml").read_text(), Path("uv.lock").read_text()
    changelog = Path("CHANGELOG.md").read_text()
    current = re.search(r'^version = "(.*)"$', pyproject, re.M)
    if not current:
        raise FragmentError("pyproject.toml has no version line")
    current = current.group(1)
    fragments = load()

    def tagged(v: str) -> bool:
        return _git("rev-parse", "-q", "--verify", f"refs/tags/v{v}").returncode == 0

    if _release_heading(current).search(changelog) and not tagged(current) and not fragments:
        # Prepared earlier (a failed check, a build to redo) — resume only by its number: a
        # keyword would step past it.
        if target != current:
            raise FragmentError(
                f"{current} is prepared but not tagged — `just release {current}` resumes it"
            )
        if lock.count(_stanza(current)) != 1:
            raise FragmentError(f"uv.lock does not record foldyard {current} — fix by hand")
        stray = _dirty(RELEASE_PATHS)
        if stray:
            raise FragmentError(f"uncommitted changes outside the release: {', '.join(stray)}")
        print(f"✓ {current} is already prepared — rechecking and rebuilding", file=sys.stderr)
        return current

    version = next_version(current, target) if target in BUMPS else target
    _parts(version)
    if tagged(version):
        raise FragmentError(f"v{version} is already tagged — releases are never re-cut")
    level = bump_level(current, version)
    if level is None:
        raise FragmentError(f"{version} is not above {current}")
    if not fragments:
        raise FragmentError("nothing to release — .changes/ holds no fragments")
    stray = _dirty(("foldyard.toml",))
    if stray:
        raise FragmentError(f"commit or stash first — uncommitted: {', '.join(stray)}")
    if lock.count(_stanza(current)) != 1:
        raise FragmentError(f"expected one foldyard {current} stanza in uv.lock")
    if _release_heading(version).search(changelog):
        raise FragmentError(f"CHANGELOG.md already has a '## {version}' section — fix by hand")
    project = tomllib.loads(Path("foldyard.toml").read_text()).get("project", {})
    if version not in project.get("foldyard_version_reasons", {}) or (
        project.get("recommended_foldyard_version") != version
    ):
        raise FragmentError(
            f"our own foldyard.toml first: a [project.foldyard_version_reasons] entry for "
            f'"{version}" (a line of this release\'s why) and recommended_foldyard_version = '
            f'"{version}" — docs/releasing.md'
        )
    for f in fragments:
        if BUMPS.index(f.bump) > BUMPS.index(level):
            print(
                f"⚠ {f.path.name} asks for a {f.bump} release; this one is a {level}",
                file=sys.stderr,
            )

    Path("pyproject.toml").write_text(
        pyproject.replace(f'version = "{current}"', f'version = "{version}"', 1)
    )
    Path("uv.lock").write_text(lock.replace(_stanza(current), _stanza(version)))
    ordered = _in_merge_order(fragments)
    Path("CHANGELOG.md").write_text(roll(changelog, render(version, date, ordered)))
    for f in fragments:
        f.path.unlink()
    print(
        f"✓ {current} → {version}: pyproject.toml, uv.lock, CHANGELOG.md "
        f"({len(fragments)} fragment{'s' if len(fragments) != 1 else ''} folded in)",
        file=sys.stderr,
    )
    return version


def main(argv: list[str]) -> int:
    try:
        match argv:
            case ["check"]:
                n = len(load())
                print(f"✓ .changes/: {n} fragment{'s' if n != 1 else ''}, all valid")
            case ["prepare", target, date]:
                print(prepare(target, date))
            case ["notes", version]:
                print(notes(Path("CHANGELOG.md").read_text(), version), end="")
            case _:
                print(__doc__, file=sys.stderr)
                return 2
    except FragmentError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
