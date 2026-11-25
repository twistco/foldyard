---
section: Changed
bump: patch
---

- **`fy box down` is refused in a box.** Run by an agent, it removed the box the agent was running
  in, and without a terminal it didn't ask. With `WORKTREE=<name>` it removed another worktree's
  box, and any session in it. Run it on your computer, as `fy box recreate` already had to be.
