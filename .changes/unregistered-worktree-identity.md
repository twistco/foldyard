---
section: Security
bump: patch
---

- **A folder the box makes beside your worktrees is now filed under your project, not under its own
  name.** `fy tui` lists such a folder without reading it, but selecting it named a project after
  the folder: a name like `bad name` took the TUI down, and any other name pointed the card at
  `~/.foldyard/<that name>/`, another project's state if the box picked one, and claimed a daemon
  port range for it on your computer. Its card now reads your project's state, under its own
  worktree entry, and still runs nothing.
