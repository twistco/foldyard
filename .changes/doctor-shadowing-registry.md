---
section: Fixed
bump: patch
---

- **In a dev box, `fy doctor`'s `box shadowing` row no longer tells you to recreate the box for a
  worktree your computer never registered.** A checkout made with plain `git worktree add` (or
  before the registry existed) is masked in no box, because a box masks only the worktrees
  registered with your computer. But the box can't read the registry, so its doctor counted every
  checkout it could see and reported one like that as "added after the box was created", with a
  recreate that could never fix it — even for a box created seconds earlier. The row now reads the
  registry your computer baked into the box when it created it, lists unregistered checkouts
  apart, and gives the real fix: `fy worktree add <name>` on your computer (it asks before
  registering an existing checkout), then recreate the box. `fy box up` on a running box says the
  same. A box created by an older foldyard carries no such record, so its row still can't tell the
  two apart, and now says so.
