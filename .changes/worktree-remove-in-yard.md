---
section: Security
bump: minor
---

- **`fy worktree remove` no longer reads the worktree's files with git on your computer.** To tell
  whether a worktree had uncommitted work, and to save that work on `refs/fy/removed/<name>-<time>`
  under `--force`, git compared or stored every file, and in doing so ran any `clean` filter driver
  the shared `.git/config` names. A box can write that config, so removing a worktree could run
  box-planted code on your computer. For a worktree whose files were checked out in the VM, that
  meant every file. The check and the save now run in a one-off container from your box image,
  with no network, and git on your computer only writes the ref and deletes the checkout. So
  `fy worktree remove` now needs the box image and the VM. Without them it stops before it has
  taken anything down and prints what to run. If you're sure nothing uncommitted in the worktree
  matters, the new `--discard` removes it without checking or saving anything. A check or save
  that fails now keeps the worktree instead of removing it unsaved.
- **`fy worktree remove`'s "uncommitted work" now means files that differ from the last commit
  once everything is unstaged.** It used to be git's own check, which also counts the stage. Your
  computer's stage for a worktree goes stale whenever its box commits, so a worktree whose files
  matched its last commit could still be refused. That no longer happens, and whatever the check
  does call uncommitted, `--force` saves. An untracked file counts even when
  `status.showUntrackedFiles` is `no`. A change that exists only in the stage (staged, then undone
  in the file) isn't counted or saved, just as unstaging would lose it.
