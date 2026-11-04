"""``just pin <commit>``: ``fy`` frozen at one commit, so switching branches in the checkout you
develop in doesn't change the tool you're running.

An editable install from a private clone (``~/.local/share/fy-pin``, or ``FY_PIN_DIR``), not
``uv tool install git+file://…@<hash>``: only an editable host install stages a wheel for the box
(``box._foldyard_install_subst``), so a git install would put PyPI's release of the same version
number in the box, without any commit since it.

Any commit in the repo will do, pushed or not, on any branch or none. A hash, never a name: a box can
move a branch, so a name can resolve to a commit nobody read. The repo is one a box can write, so:

- nothing in its config runs here (hooks, fsmonitor, a lazy fetch). Git commands here stick to
  ones that never touch its working tree, whose filter drivers its config also names.
- a short hash is looked up among the objects. ``rev-parse`` tries refs first, so a branch named
  like the hash would win. Seven hex digits can still be brute-forced, so the report prints the full
  hash and subject, read from the clone. A full hash is safe from all of this: git checks what it
  fetches against the hash.
- the clone takes commits from it and nothing else, so its config stays yours. Point the clone's
  ``origin`` at GitHub and only pushed commits can be pinned.

Installing runs the commit's build step here, so the hash is the gate: read what you pin. This script
and its recipe also live in the checkout, like every recipe.

Stdlib only: ``uv run --no-project`` runs it.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PIN_DIR_ENV = "FY_PIN_DIR"
_HASH = re.compile(r"[0-9a-fA-F]{7,40}")
_FULL_HASH_LENGTH = 40
_NOT_PRINTABLE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_REASON_LINES = 12  # uv's root cause is the last line of its report

# As foldyard.worktree's, which this can't import under `--no-project`. Not GIT_PROTOCOL_FROM_USER=0:
# it would refuse the local path `origin` is. A precaution, not the mechanism: the commands here don't
# reach these today, and test_nothing_in_the_repos_config_runs passes without them. Choosing those
# commands is what keeps the repo's config from running, so a new git command here needs that test.
_NO_REPO_CODE = (
    "--no-replace-objects",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=",
    "-c",
    "protocol.ext.allow=never",
)
_NO_FETCH_ENV = {"GIT_NO_LAZY_FETCH": "1"}
# Set by a git hook (and by `git rebase --exec`): each wins over -C, so a run from one would init,
# fetch and check out in the repo the hook belongs to.
_REPO_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
    "GIT_NAMESPACE", "GIT_PREFIX",
)  # fmt: skip


class Refused(Exception):
    """Nothing was pinned, and this says why."""


@dataclass(frozen=True)
class Pinned:
    commit: str
    subject: str
    previous: str | None


def clean(text: str) -> str:
    """Box-written text with what a terminal would act on taken out."""
    return _NOT_PRINTABLE.sub("", text)


def _clean_lines(text: str) -> str:
    return "\n".join(clean(line) for line in text.strip().splitlines()[-_REASON_LINES:])


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _REPO_VARS} | _NO_FETCH_ENV


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *_NO_REPO_CODE, "-C", str(cwd), *args],
        env=_env(),
        capture_output=True,
        text=True,
        errors="replace",
    )


def _must(result: subprocess.CompletedProcess, doing: str) -> str:
    if result.returncode != 0:
        raise Refused(f"couldn't {doing}:\n{_clean_lines(result.stderr)}")
    return result.stdout.strip()


def _commits_starting(repo: Path, prefix: str) -> list[str]:
    """The commits among the objects with this prefix: no ref is consulted."""
    found = _git(repo, "rev-parse", f"--disambiguate={prefix}")
    if found.returncode != 0:
        return []
    return [
        oid
        for oid in found.stdout.split()
        if _git(repo, "cat-file", "-t", oid).stdout.strip() == "commit"
    ]


def resolve(
    repo: Path, commit: str, candidates: Callable[[Path, str], list[str]] = _commits_starting
) -> str:
    """The full hash ``commit`` names. A full one is taken as it is, since it may be on a clone's
    ``origin`` that isn't this repo; the fetch is what proves it exists."""
    if not _HASH.fullmatch(commit):
        raise Refused(
            f"{clean(commit)!r} isn't a commit hash. A box can move a branch, so read the commit "
            "and pin its hash."
        )
    commit = commit.lower()
    if len(commit) == _FULL_HASH_LENGTH:
        return commit
    found = candidates(repo, commit)
    if not found:
        raise Refused(f"no commit in {repo} starts with {commit}")
    if len(found) > 1:
        raise Refused(f"more than one commit starts with {commit}: use the full hash")
    return found[0]


def _ensure_clone(directory: Path, repo: Path) -> None:
    """Made with `init` and a fetch of the one commit, not `clone`: another branch's damage can't
    stop this one being pinned, and a failure leaves nothing half-made to clean up."""
    if (directory / ".git").exists():
        return
    if directory.exists() and any(directory.iterdir()):
        raise Refused(f"{directory} isn't a clone of foldyard, and has files in it: left alone")
    directory.mkdir(parents=True, exist_ok=True)
    _must(_git(directory, "init", "-q"), f"make a clone in {directory}")
    _must(_git(directory, "remote", "add", "origin", str(repo)), f"set up {directory}")


def _head(directory: Path) -> str | None:
    head = _git(directory, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    return head.stdout.strip() if head.returncode == 0 else None


def _checkout(directory: Path, commit: str) -> None:
    _must(_git(directory, "checkout", "--quiet", "--detach", commit), f"check out {commit}")


def install(directory: Path) -> None:
    """From `/`: anywhere under the checkout, uv would read a `uv.toml` a box can write, which can
    name another package index."""
    argv = ["uv", "tool", "install", "--force", "--editable", str(directory)]
    print(f"▶ {shlex.join(argv)}", flush=True)
    done = subprocess.run(argv, cwd="/", capture_output=True, text=True, errors="replace")
    if done.returncode != 0:
        raise Refused(f"`uv tool install` failed:\n{_clean_lines(done.stderr)}")


def installed_from(directory: Path) -> bool:
    """Is the `foldyard` tool an editable install of this directory? uv's receipt says, and nothing
    else can: the clone's HEAD stays put through a `just install` or a failed first pin, when `fy`
    is running something else. From `/` for the reason :func:`install` gives. Unknown counts as no,
    since this decides whether we may call `fy` pinned."""
    try:
        asked = subprocess.run(
            ["uv", "tool", "dir"], cwd="/", capture_output=True, text=True, errors="replace"
        )
    except OSError:
        return False
    if asked.returncode != 0:
        return False
    try:
        receipt = Path(asked.stdout.strip()) / "foldyard" / "uv-receipt.toml"
        requirements = tomllib.loads(receipt.read_text())["tool"]["requirements"]
    except (OSError, ValueError, KeyError):  # TOMLDecodeError is a ValueError
        return False
    return any(
        req.get("name") == "foldyard"
        and req.get("editable")
        and Path(req["editable"]).resolve() == directory.resolve()
        for req in requirements
    )


def pin(
    commit: str,
    repo: Path,
    directory: Path,
    install: Callable[[Path], None] = install,
    installed_from: Callable[[Path], bool] = installed_from,
) -> Pinned:
    if directory.resolve().is_relative_to(repo.resolve()):
        raise Refused(f"{directory} is inside {repo}: a pin has to be outside what a box can write")
    full = resolve(repo, commit)
    _ensure_clone(directory, repo)
    previous = _head(directory) if installed_from(directory) else None
    _must(
        _git(directory, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "origin", full),
        f"fetch {full}",
    )
    _checkout(directory, full)
    try:
        install(directory)
    except Refused as e:
        if previous is not None:
            _checkout(directory, previous)
        if previous is not None and installed_from(directory):
            raise Refused(f"{e}\n`fy` is still pinned to {previous}.") from e
        raise Refused(f"{e}\n`fy` is not pinned.") from e
    if not installed_from(directory):
        raise Refused(
            f"uv installed {directory}, but `fy` isn't reported as running it: not pinned."
        )
    subject = _must(_git(directory, "log", "-1", "--format=%s", full), f"read {full}")
    return Pinned(full, clean(subject), previous)


def report(pinned: Pinned) -> str:
    lines = [f"✓ fy is pinned to {pinned.commit}", f"    {pinned.subject}"]
    if pinned.previous and pinned.previous != pinned.commit:
        lines.append(f"  (was {pinned.previous})")
    lines += [
        "  Daemons already running have the old code: `fy host restart` in each project.",
        "  `just install` puts `fy` back on this checkout.",
    ]
    return "\n".join(lines)


def pin_dir() -> Path:
    configured = os.environ.get(PIN_DIR_ENV)
    return Path(configured) if configured else Path.home() / ".local" / "share" / "fy-pin"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Freeze `fy` at one commit of this repo.")
    ap.add_argument("commit", help="its hash, pushed or not")
    args = ap.parse_args(argv)
    try:
        if os.environ.get("IN_DEVBOX") == "1":
            raise Refused("in a box: this is your computer's `fy`, so pin it from there")
        common = _must(
            _git(Path.cwd(), "rev-parse", "--path-format=absolute", "--git-common-dir"),
            "find this repo",
        )
        print(
            report(
                pin(
                    args.commit,
                    Path(common).parent,
                    pin_dir(),
                    install=install,
                    installed_from=installed_from,
                )
            )
        )
    except Refused as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
