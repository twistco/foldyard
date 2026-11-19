---
section: Fixed
bump: patch
---

- **A worktree on a branch from before a `[ports]` pool gets its pooled port wherever `fy` runs.**
  The pool was read from the config of the checkout `fy` ran in, so one worktree published two
  ports: its pooled port with `WORKTREE=<name> fy up` from main, and its offset port with `fy up`
  inside it, whose branch's `foldyard.toml` had no pool yet (the TUI and the supervisor read it the
  second way too). The pool now comes from the main checkout's `[ports]`, as `[worktree-offsets]`
  pins do: its adopted config on your computer, its working tree in a box.
