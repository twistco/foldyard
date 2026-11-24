---
section: Changed
bump: patch
---

- **In a box, `fy worktree remove` takes any worktree your computer never registered, and says why
  when it won't.** It took only the ones a box had made, so a plain `git worktree add` one from
  before that was refused with "run on your computer". Your computer now publishes the names of its
  registered worktrees in the to-box: those are refused, with the command to run there, and any
  other in the worktrees root is removed, with the same check for uncommitted work.
  `fy worktree list` in a box shows which is which. The supervisor publishes the list:
  `fy host restart` after upgrading, then `fy box recreate` so the box has the new foldyard.
