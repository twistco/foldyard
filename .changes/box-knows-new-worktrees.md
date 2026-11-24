---
section: Fixed
bump: patch
---

- **A running box works with a worktree registered since it was made.** With a `[ports]` pool,
  `WORKTREE=<name> fy box exec`, `fy ps` and `fy shellenv` for one were refused in every older box
  ("this box doesn't know its ports"), and the advice, `fy box recreate`, ends the sessions in it.
  The box now reads its ports from the list your computer publishes in the to-box.
- **Registering an existing worktree says what it does to the boxes using it, and offers its box.**
  Its dependency dirs go read-only in every other box, so a session working in it there could no
  longer install or test, with no box of its own to go on in. `fy worktree add <name>` now says so
  before it asks, and offers to start that box.
