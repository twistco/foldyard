---
section: Fixed
bump: patch
---

- **A box git command that works no longer prints "No such file or directory".** The box's git
  wrapper leaves small notes for your computer next to the git index, and a note it can't write
  is meant to be skipped silently. On a Podman machine a note your computer had just cleared up
  can't be written again for a few seconds, and the error from that attempt still reached the
  terminal, so a clean `git status` looked like a failure.
