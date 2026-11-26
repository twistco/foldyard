---
section: Added
bump: minor
---

- **`fy config features` shows what foldyard can do and what your config uses.** It lists features
  by the release that added them, newest first, so a project set up under an older foldyard can see
  what arrived since. The releases at or below `[project] recommended_foldyard_version` sit under a
  line. Then it lists the keys foldyard won't act on as written: an unknown key, with the nearest
  real one (a typo used to be silent), a removed or renamed key, and an old default written out. It
  exits 1 on an unknown or removed key. `fy ls --features` shows every project side by side.
