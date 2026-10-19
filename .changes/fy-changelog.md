---
section: Added
bump: minor
---

- **`fy changelog` prints what changed between the foldyard your `foldyard.toml` declares and the
  one installed.** A project can sit any number of releases behind the `fy` running it, and
  catching its config up meant reading the whole changelog on GitHub. Now the releases in between
  (after `min_foldyard_version`, or `recommended_foldyard_version` if higher, up to the installed
  version) print oldest first, from the changelog the install itself ships, so it works offline
  and inside the box. `--action` keeps only what can need a step (each release's summary plus
  Security, Removed, Deprecated and Changed); `--since`/`--until` pick the range by hand and work
  outside a project. Raise `min_foldyard_version` once the config is caught up and the next run
  starts there. `fy doctor`'s version row points at it when the install is ahead of the repo.
