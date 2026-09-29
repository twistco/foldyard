---
section: Fixed
bump: patch
---

- **A `git pull --rebase` that stopped before it began now says so, and how to recover.** When
  git can't start the rebase after it has set your uncommitted changes aside (its "autostash"),
  it leaves `.git/rebase-merge` holding just those changes. It isn't a rebase in progress, so
  `git rebase --abort` can't clear it, every later `git pull` refuses to start, and the box
  refused every commit with "run it again once it's done", which never came. The box now names
  it and points you at `fy doctor`, which shows the commands to run on your computer (one row per
  checkout, worktrees included): they keep your changes in `git stash list` first, then remove the
  directory, then restore the changes by their stash id. foldyard only prints them; it never runs
  git in your checkout. The box's new message arrives once the box is recreated
  (`fy box down && fy box up`).
