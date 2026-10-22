---
section: Fixed
bump: patch
---

- **An install in another checkout's dependency dir from a dev box now fails, with a note
  pointing at that checkout's own box, instead of landing on your computer's disk.** Every box
  sees the main checkout and every worktree, but `[box].shadow_volumes` shadowed only the box's
  own checkout: a `pnpm install` run in a worktree from main's box (or in main from a worktree's
  box) wrote real files into the checkout on your computer — about three times slower over the VM
  mount, and shared with your own tools. Now each box mounts those dirs of every other checkout
  read-only; the one file in them, `RUN-IT-IN-ITS-OWN-BOX`, says to run the command there with
  `WORKTREE=<name> fy box exec '<command>'` (`WORKTREE= …` for the main checkout). A box's mounts
  are fixed when it is created, so this applies after `fy box down && fy box up`; a worktree
  added later isn't masked in boxes already running, and `fy worktree add` names them. `fy doctor`
  has a new `box shadowing` row that asks the running box what it actually mounts (the `shadow
  volumes` row only ever read the config), and `fy box up` on a running box says what it lacks
  (#91).
