---
section: Fixed
bump: patch
---

- **A long-running box no longer crashes its agents.** The box's first process was
  `sleep infinity`, which never collects the processes left to it, and every `git commit` leaves
  one behind (git's background maintenance). They piled up for the box's whole life, and once
  the box reached its limit of 2048 processes, a program that couldn't start a thread aborted:
  Claude sessions died with "core dumped", several at once. A box is now created with `--init`,
  so they are collected as they exit. A running box keeps the old behaviour until
  `fy box down && fy box up`.
