---
section: Added
bump: minor
---

- **`fy worktree add <name>` works in a box: an agent makes its own worktree, with nobody asked.**
  It used to be refused there, so agents fell back on plain `git worktree add`, where every install
  lands on your computer's disk, about 3× slower than a volume, and (on macOS) holds a file open for
  each file. In a box it makes the checkout in the worktrees root, with volumes over its dependency
  dirs (`[box].shadow_volumes`), to use from that box; `fy worktree remove` there removes it. It has
  no stack, ports or mode: the same command on your computer (`--box` for a box of its own)
  registers it in place and keeps the dependencies already installed. `fy docs git` has the details,
  and replaces the earlier advice to use `.claude/worktrees/`.
