---
section: Fixed
bump: patch
---

- **A worktree that isn't adopted yet is still its project's on your computer.** With nothing
  adopted your computer reads no config for a checkout, so a fresh worktree was named after its
  directory: a project of that name (its own `~/.foldyard/` state dir and port band), a machine
  of that name and the default `lima` backend. A `fy box exec` there was told "lima machine
  '<worktree>' does not exist" on a podman host, and `fy mode` or `fy allow` wrote state no
  checkout reads. For a worktree `fy worktree add` registered, `[project] name`/`prefix` and
  `[machine] name`/`backend`/`worktrees_root` now come from main's adopted config. Nothing else
  does: your computer still runs nothing for the worktree until it is adopted.
