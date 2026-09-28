---
section: Fixed
bump: patch
---

- **A box's first git command no longer sees every file as a staged deletion.** It copies your
  computer's git index to start from, and a git command on your computer that had just replaced
  that file made it look missing from the box for up to a second (up to five on a Podman
  machine). The box then started from an empty index. It now retries the copy briefly, and if
  the index still isn't there it starts from the last commit. A commit on your computer hides the
  branch in the same window, so that no longer passes for a new repository with nothing to copy,
  and two first git commands racing in the box no longer overwrite each other's staging.
