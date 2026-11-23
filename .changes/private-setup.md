---
section: Added
bump: minor
---

- **`fy init --private`, for a project that doesn't use foldyard.** One command now does what
  `fy docs private-config` used to list as manual steps:

  - It writes `[project] private = true`.
  - It hides foldyard's files with the clone's own `.git/info/exclude`, and leaves `.gitignore`
    alone.
  - It refuses a clone where git already tracks a foldyard file.
  - On a clone set up the usual way, it converts the setup and keeps your `foldyard.toml`.

  In such a setup:

  - `fy worktree add` and `fy worktree init` write main's adopted config and the foldyard skill into
    the worktree from your computer, so the `.fy/worktree-init.sh` script is no longer needed.
  - Each agent's system prompt gets foldyard's own note saying the files are private.
  - `fy doctor` fails if git tracks one of them.
