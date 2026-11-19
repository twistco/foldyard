---
section: Added
bump: minor
---

- **A `[ports]` key can give worktrees their port from a pool:**
  `WEB_PORT = { base = 3000, worktrees = "3001-3005" }`. A worktree's offset moves all of its ports,
  so a worktree couldn't use a service that accepts only a few, such as an OAuth tenant's callback
  URLs: it landed on `:3042` and couldn't log in, and changing its offset afterwards moved every
  port, its proxy and token-service ports included. `fy worktree add` now gives the new worktree the
  lowest free port of the pool and refuses, naming each holder, when none is free. Only that key
  leaves the offset. Worktrees registered before the pool keep their offset port (`fy doctor`'s
  `ports pool` row names them; `fy worktree add <name>` again moves one), and a box refuses a
  worktree added after it was made rather than guess its port. An older foldyard reads a pooled key
  as missing, so set `min_foldyard_version` when you add one.
