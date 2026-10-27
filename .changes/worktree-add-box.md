---
section: Added
bump: minor
---

- **`fy worktree add <name> --box` brings the new worktree's box up as well**, the same as running
  `WORKTREE=<name> fy box up` afterwards. It starts the box only once the worktree's config is
  adopted, and says so when it isn't; on a worktree that already exists it starts the box, and
  that box's own launch check asks for anything still to adopt.
