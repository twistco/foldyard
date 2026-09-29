---
section: Fixed
bump: patch
---

- **Git in the box no longer loses, rewinds or reverts commits made on your computer.** Over the
  VM mount the box can briefly read a branch your computer just moved as older, or as having no
  commits — for up to ~5 s on a Podman machine, up to ~1 s on Lima. In that window a box commit
  could land on an old commit or start a new history, a box `git reset` could move your branch
  back over your latest commits, and a commit on your computer right after the box's could record
  the box's new files as deleted. The box's git wrapper now checks every branch move while git
  holds the branch's lock, and updates your computer's staging area as part of the box's own
  commit. When the two sides collide it refuses with "foldyard git shim: … Nothing was changed;
  run it again" instead.
- **Git hooks run in the box as they do on your computer.** A repo hook's own `git` (lefthook
  runs a couple of dozen per commit) now goes through the box's git wrapper. Before, in the
  moments after your computer committed, it could see every file in the repo as staged, and
  formatters ran on all of them. lefthook no longer tries to install its hooks into foldyard's
  own directory either, which printed "could not replace the hook: permission denied" on some
  box commits.
- **A box commit no longer moves your branch under a rebase or merge your computer has
  started**, including one that began after the box first checked, which made your `git pull
  --rebase` fail to finish. The box refuses until your operation is done.
