"""This repo's own pre-commit hook (`.githooks/pre-commit`; DEVELOPMENT.md).

Git runs a hook only if it is executable, and skips one that isn't without a word. Clones of this
repo have `core.fileMode = false` (the shared mount), so git records a new file's mode as it is
told, not as the disk has it: the hook went in as 100644 and every box skipped it, which only a
live commit showed (2026-11-11). `git update-index --chmod=+x` records it; this keeps it so.

Read from the committed tree, not an index: a box's git keeps its own (ADR-0021's index split),
so the same `ls-files` gives different answers in a box and on your computer until a commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_pre_commit_hook_is_recorded_executable():
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-tree", "HEAD", "--", ".githooks/pre-commit"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out, "no .githooks/pre-commit committed"
    assert out.split()[0] == "100755", f"recorded {out.split()[0]}: git would skip it — " + (
        "`git update-index --chmod=+x .githooks/pre-commit`"
    )
