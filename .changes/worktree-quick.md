---
section: Added
bump: minor
---

- **`fy worktree quick <name>`: an agent makes its own worktree, from the box.** `fy worktree add`
  needs you, so agents fell back on plain `git worktree add`, where every install lands on your
  computer's disk, about 3× slower than a volume, and (on macOS) holds a file open for each file. A
  quick worktree sits in the worktrees root with the rest, has volumes over its dependency dirs
  (`[box].shadow_volumes`) and runs in the box that made it. `fy worktree remove`, in the box,
  removes it. It has no stack: `fy worktree add <name> --box` on your computer promotes it, and its
  box keeps the dependencies already installed. `fy docs git` has the details, and what replaces the
  earlier advice to use `.claude/worktrees/`.
