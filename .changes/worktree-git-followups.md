---
section: Security
bump: patch
---

- **`fy tui`, `fy worktree list` and `fy worktree remove` no longer run git inside a worktree.** A
  worktree's branch is now read from your main checkout's `.git`, and its main checkout is the one
  your computer recorded it under. A box can rewrite a worktree's `.git` file, even for one you made
  with `fy worktree add`. Git run inside the worktree then read a git directory and config the box
  built: a file there that never finishes reading froze `fy tui`, and it could make foldyard take
  another checkout as that worktree's main.
- **The git that finds your main checkout and checks its `core.fileMode`, the git behind `fy tui`'s
  cards and `fy worktree list`, and `fy worktree add` and `remove` now give up when git doesn't
  answer.** The box can write your main checkout's `.git` too, and git waits for good on a file
  there that never finishes reading, which froze `fy tui` and every verb. Now a branch is shown
  blank, `core.fileMode` is left as it is, and the rest say git didn't answer: a checkout is then
  read as no repository, and `fy worktree remove` removes nothing and tears nothing down, even with
  `--discard`.
