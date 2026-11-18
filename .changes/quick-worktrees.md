---
section: Added
bump: patch
---

- **Agents can make a quick worktree in their box, and the docs say how.** `fy docs git` and the
  bundled foldyard skill now describe a plain
  `git worktree add --no-track -b <branch> .claude/worktrees/<name>` from a box for a small change
  that needs no stack: safe inside the checkout, whose path is the same on both sides, and nowhere
  else. `--no-track` keeps a branch started from `origin/main` from tracking it. Agents had avoided
  it because the docs only said not to run `git worktree add` in a box. The in-box refusal of
  `fy worktree add` now points to it, and `fy init`'s managed `.gitignore` block adds
  `.claude/worktrees/`. In an existing project, add that line to `.gitignore` (or
  `.git/info/exclude`) yourself.
