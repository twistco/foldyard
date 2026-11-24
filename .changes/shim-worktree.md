---
section: Changed
bump: minor
---

- **In a box, git points a plain `git worktree add` at `fy worktree add`.** Outside the checkout
  (the worktrees root, `/tmp`) it is refused with the command to use, and so is
  `git worktree prune`, which drops the record of every worktree the box can't see. Inside the
  checkout, where agents' own worktree tools put theirs, it is made with a warning not to install
  there. Only the box's own project; `FY_GIT_SHIM_OFF=1` gets past it. A script that makes a
  worktree elsewhere in a box needs that, or `fy worktree add`.
