"""Your computer's git identity, read on the host so the dev box commits as you.

Git takes ``user.name`` / ``user.email`` from the environment, then the repo's own config, then
the global config (``~/.gitconfig``). The box has the first two — its env and the shared checkout
— but its HOME is rebuilt with every box, so the global layer is empty there and an agent in the
box has no name to commit under. ``box._git_identity_step`` writes these two keys into the box's
global config, each only where the box doesn't already have one; nothing else from your global
config is carried (a credential helper or a signing key would point at tools the box lacks).

Read from your GLOBAL config only, never the checkout's: its ``.git/config`` is writable from the
box (ADR-0022), so an ``includeIf gitdir:`` identity — matched against the checkout's path — is
not followed; a plain ``include.path`` is. Host facts, not repo config. Each value ends up in the
bootstrap script (shell-quoted there), so a blank one or one holding a control character is
dropped rather than passed. Stdlib-only.
"""

from __future__ import annotations

import os
import subprocess

KEYS = ("user.name", "user.email")
# What would point git at a repository despite the cwd: an inherited one (a git hook's, say) lets
# `includeIf gitdir:` match the checkout.
_REPO_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
_TIMEOUT_S = 5


def conditional_includes() -> bool:
    """Whether your global git config (or a file it includes) has any ``includeIf`` rule — the
    per-directory identities this module deliberately doesn't follow, which the box warns about.
    False when git isn't installed or there's no global config."""
    got = _git("--name-only", "--get-regexp", r"^includeif\.")
    return got is not None and got.returncode == 0 and bool(got.stdout.strip())


def identity() -> dict[str, str]:
    """``user.name`` and ``user.email`` from your global git config, each only when set to a
    usable value; ``{}`` when git isn't installed or names neither."""
    found: dict[str, str] = {}
    for key in KEYS:
        if (value := _global(key)) is not None:
            found[key] = value
    return found


def _global(key: str) -> str | None:
    got = _git("--get", key)
    if got is None or got.returncode != 0:
        return None
    value = got.stdout.removesuffix("\n")
    if not value.strip():
        return None
    return None if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value) else value


def _git(*args: str) -> subprocess.CompletedProcess[str] | None:
    """``git config --global --includes <args>``, or None when git can't be run or answers in
    something other than UTF-8."""
    try:
        # cwd `/` and no repository in the env: nothing of the checkout's can take part.
        got = subprocess.run(
            ["git", "config", "--global", "--includes", *args],
            cwd="/",
            env={k: v for k, v in os.environ.items() if k not in _REPO_ENV},
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
        return subprocess.CompletedProcess(got.args, got.returncode, got.stdout.decode("utf-8"))
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
