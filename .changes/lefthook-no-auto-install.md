---
section: Changed
bump: patch
---

- **Repo hooks in a box run with `LEFTHOOK_NO_AUTO_INSTALL=1`, so a lefthook newer than 2.2.2 skips
  its hook sync there.** That sync is what used to write lefthook's scripts into foldyard's own
  hooks directory. A lefthook with the variable (evilmartians/lefthook#1586) leaves your own
  `lefthook run` syncing as before, which the `no_auto_install` key wouldn't. Older lefthook ignores
  the variable and keeps foldyard's existing workaround. Boxes pick this up at `fy box recreate`.
