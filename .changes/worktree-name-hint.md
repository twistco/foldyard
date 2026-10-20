---
section: Changed
bump: patch
---

- **`fy worktree add` suggests a directory name when given a branch-shaped one.** The first
  argument is the worktree's directory, so `fy worktree add sec/vuln-bumps` is refused (a nested
  directory would never be discovered). The refusal now suggests
  `fy worktree add sec-vuln-bumps sec/vuln-bumps` instead of leaving you to work out which
  argument was wrong.
