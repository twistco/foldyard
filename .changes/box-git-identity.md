---
section: Added
bump: patch
---

- **A new dev box commits as you: it gets your computer's git `user.name` and `user.email`.** The
  box's home directory is new with every box, so it had no global git config, and an agent in a
  repo with none of your commits to copy from committed under a made-up name. `fy box up` now
  writes those two keys from your global git config into the box's, each only where the box has
  none — so `GIT_AUTHOR_*` in `[box].env` and a repo's own `.git/config` still win, as they do on
  your computer. Nothing else from your git config is carried, and the checkout's config is never
  read for it, so an `includeIf` identity isn't followed: when your global config has one, a new
  box says so as it sets up, unless the repo sets its own email. Applies to boxes created from now on (`fy box down && fy box up`).
