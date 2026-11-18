---
section: Changed
bump: patch
---

- **Repo hooks in a box run with `LEFTHOOK_NO_AUTO_INSTALL=1`, so a lefthook that supports it skips
  its hook sync there.** That sync is what used to write lefthook's scripts into foldyard's own
  hooks directory. Support for the variable is merged upstream (evilmartians/lefthook#1592) but not
  in a release yet: 2.2.2 and older ignore it and keep foldyard's existing workaround. A lefthook
  with it leaves your own `lefthook run` syncing as before, which the `no_auto_install` key
  wouldn't. Boxes pick this up at `fy box recreate`.
