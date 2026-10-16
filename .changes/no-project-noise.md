---
section: Changed
bump: minor
---

- **Outside a foldyard project, `fy` says so instead of inventing one.** With no `foldyard.toml`
  in the current directory or above (and no `FOLDYARD_REPO`), a project verb (`fy mode`,
  `fy doctor`, `fy up`, `fy host`…) used to run as a project named after the directory, writing
  a mode file and allocating a port band for it in `~/.foldyard/ports.json`. It now prints one
  line pointing at `fy init` and `fy prereq`, exits 1 and writes nothing. That includes a git
  repo that has no `foldyard.toml`.
- **Help, `fy prereq`, `fy docs`, `fy init`, `fy machine base` and `fy skill list` work anywhere,
  quietly.** Every verb, `--help` included, used to open with "✗ not a git repository" outside a
  git repo, including `uvx foldyard prereq` in an empty directory.
