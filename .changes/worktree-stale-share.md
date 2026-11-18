---
section: Fixed
bump: patch
---

- **`fy worktree add` no longer blames a missing git when the VM can't see the new worktree.** The
  engine reports a working directory it can't enter with the same "command not found" as a missing
  binary, so a worktrees folder deleted and made again while the VM ran (the VM keeps serving the
  old one) read as "no git there? [box].image must provide git". After such a failure foldyard now
  asks whether the image's git runs at all, and when it does, says the VM's share is out of date
  and that `fy machine stop && fy up` fixes it.
