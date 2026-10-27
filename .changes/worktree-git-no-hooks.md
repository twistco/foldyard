---
section: Security
bump: patch
---

- **`fy worktree add` and `fy worktree remove` no longer run the repo's git hooks on your
  computer.** The hooks live in the shared `.git`, which every box can write, and checking out
  the new worktree ran its `post-checkout` hooks (a consumer's lefthook ones, for instance) on
  your computer, in a checkout that wasn't registered or adopted yet. foldyard's own git now runs
  with the hooks directory off, each `[hook]` entry in `.git/config` or any file it includes
  disabled (git 2.54's config hooks, which `core.hooksPath=/dev/null` alone doesn't stop) and no
  `core.fsmonitor` command, and `fy worktree add` says which hooks it skipped. Git in the
  worktree's box still runs them. Filter drivers such as Git LFS's still run on checkout; see the
  security page.
