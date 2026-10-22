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

import subprocess

KEYS = ("user.name", "user.email")
_TIMEOUT_S = 5


def identity() -> dict[str, str]:
    """``user.name`` and ``user.email`` from your global git config, each only when set to a
    usable value; ``{}`` when git isn't installed or names neither."""
    found: dict[str, str] = {}
    for key in KEYS:
        if (value := _global(key)) is not None:
            found[key] = value
    return found


def _global(key: str) -> str | None:
    try:
        # cwd `/`: no repository around it, so nothing of the checkout's can take part.
        got = subprocess.run(
            ["git", "config", "--global", "--includes", "--get", key],
            cwd="/",
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
        value = got.stdout.decode("utf-8").removesuffix("\n")
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    if got.returncode != 0 or not value.strip():
        return None
    return None if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value) else value
