---
section: Security
bump: minor
---

- **`fy worktree add` no longer checks files out on your computer.** A checkout runs the filter
  drivers `.git/config` names (Git LFS's smudge filter, or a command a box wrote there), and the
  shared `.git` is writable from every box, so making a worktree could run box-planted code on
  your computer. Git there now only records the worktree (its branch and its entry in `.git`), and
  the files are checked out in a one-off container from your box image, with no network.
  It therefore needs the box image: before your first `fy box build` it stops with the command to
  run, having changed nothing. Git LFS files arrive as pointers (`git lfs pull` in the worktree's
  box fetches them), the first `git status` on your computer in the new worktree re-reads every
  file once, and a checkout that fails removes the half-made worktree and the branch it made.
