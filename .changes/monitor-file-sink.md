---
section: Added
bump: minor
experimental: true
---

- **A monitor hook can write to a file, and `fy monitor hook add` sets one up.** Use
  `fy monitor hook add NAME --file PATH` (or `--url URL`, or `-- /abs/program args…`), with
  `--who`, `--kinds` and `--worktree` filters. Bare `fy monitor hook` lists your hooks and how
  their deliveries are going, `fy monitor hook remove` removes one, and `fy doctor` warns about a
  hook that is failing or has skipped events. Before this, hooks had to be written by hand in
  `~/.foldyard/<project>/monitor-hooks.toml`, and nothing showed whether they worked. A `file`
  hook replaces a shell command that appends into the checkout so the box can read the events.
  Such a command follows symlinks, so the box could aim your computer's writes at any of your
  files. The sink never follows a symlink the box could have made. It signs each line with a key
  only your computer holds, so `fy monitor check PATH` finds a line edited, removed, moved or
  added, or lines cut from the end. Bare `fy monitor` is unchanged.
