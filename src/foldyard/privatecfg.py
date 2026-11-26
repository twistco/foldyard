"""A private foldyard setup: foldyard on a repo that doesn't use it (ADR-0037).

``[project] private = true`` says the project itself has no foldyard: this clone's
``foldyard.toml``, ``foldyard.local.toml`` and ``.claude/skills/foldyard/`` are untracked on
purpose, hidden by the clone's own ``.git/info/exclude`` rather than ``.gitignore``, so no commit or
pull request ever carries them. Three things follow from it:

- ``fy init --private`` sets the clone up that way (:func:`write_exclude`, :func:`mark_private`,
  :func:`drop_gitignore_block`) and refuses one that already tracks a foldyard file.
- A new worktree gets its private files from your computer (:func:`sync_worktree`): main's ADOPTED
  copy, never main's working tree, which the box can write. Being byte-for-byte what main runs, it
  is then adopted without a prompt, by the same rule as any worktree identical to main.
- Each agent is told (:data:`AGENT_NOTE`, appended to its system prompt), and doctor checks that git
  still sees none of it (:func:`problems`).

Everything written into the checkout goes through :mod:`mountwrite`: the checkout and its ``.git``
are box-writable, so any path there may be a link the box planted.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

from . import config, hostgit, mountwrite

FILES = ("foldyard.toml", "foldyard.local.toml")
SKILL = Path(".claude") / "skills" / "foldyard"
TRACKABLE = (*FILES, SKILL.as_posix())
EXCLUDE_ENTRIES = ("/foldyard.toml", "/foldyard.local.toml", "/.claude/skills/foldyard/")
EXCLUDE = Path(".git") / "info" / "exclude"

AGENT_NOTE = """\
This checkout's foldyard setup is private to the user: the upstream project doesn't use
foldyard. foldyard.toml, foldyard.local.toml and .claude/skills/foldyard/ are untracked on
purpose and hidden by .git/info/exclude, not by .gitignore.
- Never commit them (no `git add -f`), never add foldyard entries to .gitignore or any
  other tracked file, and keep foldyard out of commit messages, code and PR text.
- Don't run `git clean` with -x or -X, or `git stash --all`: they delete or take away
  the ignored files.
- Changing foldyard.toml: ask the user first, saying what and why. An edit does nothing
  until the user reviews it with `fy config diff` and runs `fy config adopt` on their
  computer, so tell them when you've made one.
- `fy docs private-config` explains the setup."""


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def with_note(prompt: str) -> str:
    """``prompt`` with :data:`AGENT_NOTE` after it."""
    return f"{prompt.rstrip()}\n\n{AGENT_NOTE}" if prompt.strip() else AGENT_NOTE


# ── the config ───────────────────────────────────────────────────────────────────────────


_TABLE = re.compile(r"^\s*\[")
_PROJECT = re.compile(r"""^\s*\[\s*(?:project|"project"|'project')\s*\]\s*(#.*)?$""")
_KEY = re.compile(r"""^\s*(?:private|"private"|'private')\s*=""")


def mark_private(text: str) -> str:
    """``text`` with ``[project] private = true``, touching nothing else: the key flipped where it
    is, else added first in ``[project]``, else a ``[project]`` table put first. Proven by parsing
    both: ``ValueError`` where the edit would change anything else (dotted ``project.x`` keys, an
    inline table, a string holding what looks like the header) or ``text`` isn't TOML."""
    marked = _marked(text)
    by_hand = "add `private = true` to its [project] table yourself, then run this again"
    try:
        before, after = config.loads_toml(text), config.loads_toml(marked)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"foldyard.toml can't be edited safely ({e}): {by_hand}") from e
    project = before.get("project", {})
    if not isinstance(project, dict) or after != {
        **before,
        "project": {**project, "private": True},
    }:
        raise ValueError(f"foldyard.toml can't be edited safely: {by_hand}")
    return marked


def _marked(text: str) -> str:
    """:func:`mark_private`'s edit, unproven."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if _PROJECT.match(ln)), None)
    if start is None:
        return "[project]\nprivate = true\n\n" + text
    end = next((i for i in range(start + 1, len(lines)) if _TABLE.match(lines[i])), len(lines))
    for i in range(start + 1, end):
        if _KEY.match(lines[i]):
            lines[i] = "private = true\n"
            return "".join(lines)
    lines.insert(start + 1, "private = true\n")
    return "".join(lines)


def enabled(main: Path) -> bool:
    """Whether main's ADOPTED config declares the setup private: what your computer acts on."""
    from . import configpin

    adopted = configpin.adopted_toml(main) or {}
    project = adopted.get("project")
    return isinstance(project, dict) and project.get("private") is True


# ── git's view ───────────────────────────────────────────────────────────────────────────


# Where an inherited environment (a git hook's) would point git instead of at ``-C``.
_REPO_LOCATION = (*hostgit.REPO_ENV, "GIT_INDEX_FILE")


def _git(checkout: Path, *args: str) -> tuple[int, str]:
    """git on your computer with none of the repo's code run (``worktree._host_git``): its hooks,
    fsmonitor and fetches off, and asked of ``checkout`` whatever repo the environment names. Only
    commands that neither check a file out nor hash one."""
    from . import worktree

    try:
        env = worktree._hookless_env(checkout)
    except worktree.HookScanError as e:
        worktree._refuse_unscanned(e)
        return 1, ""
    env = {k: v for k, v in env.items() if k not in _REPO_LOCATION}
    res = worktree._host_git(checkout, *args, env=env, capture_output=True)
    return res.returncode, os.fsdecode(res.stdout or b"")


def tracked_paths(main: Path, *paths: str) -> list[str] | None:
    """Which of ``paths`` git tracks in ``main``: a file, or any file under a directory. ``None``
    when git couldn't say (a damaged index, a config it refused): never read as "none"."""
    rc, out = _git(main, "ls-files", "-z", "--", *paths)  # -z: git quotes odd names otherwise
    if rc != 0:
        return None
    files = out.split("\0")
    return [p for p in paths if any(f == p or f.startswith(p + "/") for f in files)]


def tracked(main: Path) -> list[str] | None:
    """Which of :data:`TRACKABLE` git tracks in ``main``; ``None`` when git couldn't say."""
    return tracked_paths(main, *TRACKABLE)


def unhidden(main: Path) -> list[str]:
    """Which of the private paths present in ``main`` git would still show."""
    present = [p for p in TRACKABLE if os.path.lexists(main / p)]
    if not present:
        return []
    _rc, out = _git(main, "check-ignore", "--", *present)
    ignored = {ln.rstrip("/") for ln in out.splitlines()}
    return [p for p in present if p not in ignored]


def is_main_checkout(main: Path) -> bool:
    """A main checkout: its ``.git`` a real directory (a worktree's is a file)."""
    git = main / ".git"
    return git.is_dir() and not git.is_symlink()


# ── the clone's own exclude list ─────────────────────────────────────────────────────────


def read_nofollow(root: Path, rel: Path) -> str | None:
    """``root/rel``'s text, following no link below ``root``; ``None`` when it isn't there. Raises
    ``OSError`` for a link, or for anything but a regular file of at most
    :data:`mountwrite.TEXT_LIMIT` (a FIFO the box keeps writing to would read without end), and
    ``ValueError`` for text that isn't UTF-8."""
    try:
        dfd = mountwrite._open_dir(root, rel.parts[:-1])
    except FileNotFoundError:
        return None
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
    try:
        fd = os.open(rel.name, flags, dir_fd=dfd)
    except FileNotFoundError:
        return None
    finally:
        os.close(dfd)
    try:
        return mountwrite.read_opened(
            fd, limit=mountwrite.TEXT_LIMIT, name=str(root / rel)
        ).decode()
    finally:
        os.close(fd)


def exclude_block() -> str:
    """The fenced block for ``.git/info/exclude``: what ``fy init`` puts in ``.gitignore``, plus
    the private files themselves."""
    from . import init

    return "\n".join(
        [
            init.GITIGNORE_BEGIN,
            "# foldyard, private to this clone (`fy init --private`): never commit any of it.",
            *init.GITIGNORE_ENTRIES,
            *EXCLUDE_ENTRIES,
            init.GITIGNORE_END,
        ]
    )


def _fence(*, newlines: bool = False) -> re.Pattern[str]:
    """foldyard's fenced block; ``newlines`` also takes the line breaks either side of it."""
    from . import init

    edge = r"\n?" if newlines else ""
    body = re.escape(init.GITIGNORE_BEGIN) + r".*?" + re.escape(init.GITIGNORE_END)
    return re.compile(edge + body + edge, re.DOTALL)


def write_exclude(main: Path) -> str:
    """Put :func:`exclude_block` in ``main``'s ``.git/info/exclude``, replacing an earlier one and
    keeping every other line. ``"added"`` | ``"updated"`` | ``"unchanged"``. Raises ``OSError``
    when a directory on the way is a link."""
    existing = read_nofollow(main, EXCLUDE) or ""
    block = exclude_block()
    fence = _fence()
    if fence.search(existing):
        updated = fence.sub(lambda _: block, existing, count=1)  # a function: no `\g` expansion
        if updated == existing:
            return "unchanged"
        mountwrite.write(main, EXCLUDE, updated.encode())
        return "updated"
    prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
    mountwrite.write(main, EXCLUDE, (prefix + block + "\n").encode())
    return "added"


def drop_gitignore_block(main: Path) -> str | None:
    """Take foldyard's block back out of ``main``'s ``.gitignore`` (a setup made the usual way),
    with the blank line ``fy init`` put before it. A ``.gitignore`` left empty that git doesn't
    track was ``fy init``'s own, and goes. ``None`` when there was no block. ``"linked"`` when
    ``.gitignore`` is a link, left alone like ``fy init`` leaves it: git doesn't read one, so a
    block in the file it names hides nothing, and the link may be the box's or a colleague's."""
    if (main / ".gitignore").is_symlink():
        return "linked"
    text = read_nofollow(main, Path(".gitignore"))
    if text is None:
        return None
    fence = _fence(newlines=True)
    if not fence.search(text):
        return None
    rest = fence.sub("", text, count=1)
    if not rest.strip() and tracked_paths(main, ".gitignore") == []:
        mountwrite.unlink(main, ".gitignore")
        return "removed"
    if rest and not rest.endswith("\n"):
        rest += "\n"
    mountwrite.write(main, ".gitignore", rest.encode())
    return "restored"


# ── a worktree's copy ────────────────────────────────────────────────────────────────────


def sync_worktree(main: Path, wt_root: Path, name: str) -> bool:
    """Write main's ADOPTED private files and the bundled foldyard skill into worktree ``name``,
    from your computer. Rooted at ``wt_root``, never at the worktree's own directory: the box can
    write the worktrees root, so ``name`` itself may be a link to a directory of yours, and every
    step below ``wt_root`` is walked without following one (:mod:`mountwrite`). Main's working
    tree isn't read at all. False, with the reason said, when there's nothing adopted to copy or
    the worktree can't be written safely."""
    from . import config, configpin, skills

    selected = configpin.current_dir(config.Config(repo_root=main, worktree="", toml={}))
    if selected is None:
        _err("⚠ main's config isn't adopted, so there's no reviewed config to give the worktree:")
        _err(f"  `fy config adopt` in main, then `fy worktree init {name}`")
        return False
    adopted = configpin._read_files(selected)
    wt = Path(name)
    try:
        present = set(mountwrite.listdir(wt_root, wt))
        for file in FILES:
            data = adopted[file]
            if data is not None:
                mountwrite.write(wt_root, wt / file, data)
            elif file in present:
                _err(f"  ({file} in the worktree isn't in main's adopted config: kept, so its")
                _err("  config is reviewed rather than adopted as main's)")
        skills.write_bundled(wt_root, wt / SKILL, "foldyard")
    except (OSError, ValueError) as e:
        _err(f"✗ couldn't write the private foldyard files into {wt_root / name}: {e}")
        return False
    print("▶ wrote main's adopted config and the foldyard skill into the worktree (private setup)")
    return True


# ── doctor ───────────────────────────────────────────────────────────────────────────────


def problems(checkout: Path) -> tuple[list[str], list[str]]:
    """What breaks the setup in ``checkout`` (main or a worktree: each has its own index):
    (failures, warnings). A tracked private file fails: the next push carries it. One git would
    show, or foldyard's block in ``.gitignore``, warns."""
    from . import init

    found = tracked(checkout)
    if found is None:
        return [], ["couldn't ask git which files it tracks here (`git ls-files` failed)"]
    again = "`fy init --private` again"
    if not is_main_checkout(checkout):
        again += " in the main checkout"
    fails = [f"git tracks {p}: take it out of your branch's history before you push" for p in found]
    warns = [f"git would show {p}: {again} hides it" for p in unhidden(checkout) if p not in found]
    try:
        # Git doesn't read a .gitignore that's a link, so a block in the file it names hides
        # nothing, and `fy init --private` leaves the link alone (`drop_gitignore_block`).
        linked = (checkout / ".gitignore").is_symlink()
        gitignore = "" if linked else read_nofollow(checkout, Path(".gitignore")) or ""
    except (OSError, ValueError):  # not a regular file, or not UTF-8: either may be the box's
        warns.append("couldn't read .gitignore, so foldyard's block may be in it")
    else:
        if init.GITIGNORE_BEGIN in gitignore:
            fix = f"{again} moves it out"
            if not is_main_checkout(checkout):
                fix = "take it out of this branch"
            warns.append(f"foldyard's block is in .gitignore: {fix}")
    return fails, warns
