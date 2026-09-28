---
section: Fixed
bump: patch
---

- **A mode change now updates a running stack on a Lima VM.** The supervisor's posture reconcile
  (and `fy state`'s stack row, a new worktree's init, `fy open`) found the engine only through a
  socket your shell named; Lima registers none, so a running stack read as down and a mode change
  never re-rendered it. They now use the VM's own socket when it exists, without starting it.
  CI had hidden this by exporting the socket into the commands it ran.
