---
section: Fixed
bump: patch
---

- **`fy worktree remove` refuses a worktree that holds another one.** A git worktree made inside it
  (an agent's quick worktree under `.claude/worktrees/`, say) was deleted with it, uncommitted work
  and all: `git worktree remove` takes a worktree nested in an ignored folder even without
  `--force`, and foldyard's uncommitted-work check read only the outer one. It now names the
  worktrees inside and stops before anything is torn down, looks again once the box is down (its
  agents may have made one meanwhile), and refuses when git can't list the worktrees at all.
  `--discard` deletes those checkouts too; git then lists them as prunable until
  `git worktree prune` runs.
