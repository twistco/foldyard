---
section: Fixed
bump: patch
---
- **`fy worktree add` and `fy worktree remove` no longer print git's "unable to access
  '/home/…/.config/git/ignore': Permission denied" warnings.** The one-off container they run git
  in drops every capability, so its root can't read the box image user's home, and git warned
  each time it looked for its global ignore and attributes files there. That container now gives
  git no home directory. The warnings never changed a result.
