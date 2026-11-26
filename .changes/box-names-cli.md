---
section: Security
bump: patch
---

- **Names a box chooses no longer reach your terminal as they are.** A box can name directories in
  your worktrees and in its transcripts, set a container's service label and write a branch name.
  `fy worktree list`, `fy doctor`, `fy mode workspaces`, `fy state`, `fy transcripts`,
  `fy worktree remove`, `fy up` and the supervisor's log printed those names unchanged, so control
  sequences in them reached your terminal: they could write your clipboard, or fake a line of the
  output. They are now shown as escapes. `fy host logs` also removes control sequences from every
  line, including lines older versions wrote and the output of the daemons it runs.
- **`fy worktree list`, and `fy worktree add` or `remove` on a worktree your computer hasn't
  registered, no longer run git in it.** A box makes those directories and writes the `.git` file
  that points git at its repository data, so git there read files the box chose. The branch is now
  read from the main checkout's own record of that worktree.
