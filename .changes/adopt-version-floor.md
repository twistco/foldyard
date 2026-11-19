---
section: Fixed
bump: patch
---

- **Adopting a config that asks for a newer foldyard no longer locks every `fy` out.** On your
  computer `min_foldyard_version` is read from the ADOPTED config, and it stopped every verb,
  `fy config adopt` included. So adopting a branch's config that asked for an unreleased version, as
  the launch gate offers to, left no way back: checking the old branch out changed nothing, and only
  editing `~/.foldyard/adopted` by hand did. Adoption (the launch gate's prompt, `fy config adopt`,
  the TUI) now refuses a floor the running foldyard doesn't meet and says to upgrade first, and the
  `fy config` verbs are never behind the floor, so an adopted config can always be put right.
  Already locked out on an older foldyard? Point `adopted.json`'s `generation` in
  `~/.foldyard/adopted/<checkout>-<hash>/` back at the previous `gen-…` folder.
