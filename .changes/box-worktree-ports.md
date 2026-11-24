---
section: Fixed
bump: patch
---

- **With a `[ports]` pool, commands work in a worktree a box made.** `fy shellenv`, and so every
  recipe that starts with it, was refused there ("this box doesn't know its ports"), and the advice,
  `fy box recreate`, couldn't help: only a registered worktree has a pool port. One a box made has
  no stack, so its ports now come from its offset, and `fy up` there is still refused. For any other
  worktree the box doesn't know, the refusal now also says to register it on your computer if it
  isn't.
