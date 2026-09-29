---
section: Added
bump: patch
---

- **`fy doctor` in the box checks that box git is foldyard's git shim.** Every box-side git
  protection, from the separate index to the checks that stop the box moving a branch your
  computer just moved, lives in the shim at `/usr/local/bin/git`. A box image that puts another
  git first on `PATH`, or a shim whose hooks didn't install, used to switch all of that off with
  no sign. Two new rows say so. `git shim` fails when `git` isn't the shim, and `git shim hooks`
  fails when its hooks are missing. It warns when a user other than root could rewrite them. Each
  row names its fix. No rows when `[box] git_index_split = false`. A new box now runs `fy doctor`
  once when it's created, as the last lines of `fy box up`, so it reports this without being
  asked. (ADR-0021.) Its `egress proxy CA` row now says the box must be recreated too: the CA
  is mounted when a box is created, so `fy host restart` alone never reached an existing box.
