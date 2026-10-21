---
section: Fixed
bump: patch
---

- **A running box whose environment no longer matches the config now says so.** A box's
  environment is fixed when it's created, so an `[[inject]]` row, a `[box].env` entry or a
  timezone change adopted afterwards never reached it — and `fy box up` printed "already up" while
  the new variable was simply absent. `fy box up` on a running box now prints one warning naming
  each variable that's missing, changed or no longer set (values only for `TZ` and `LC_TIME`),
  with the fix: `fy box down && fy box up`. `fy mode`, `fy state` and the TUI make the same
  comparison, which used to cover only the keyless Claude and Codex dummies, and their fix line
  said `fy box up`, which keeps a running box as it is. A timezone or time-locale change on your
  computer is shown too, with its old → new value, but only as a warning: `fy state` still passes
  on it. It exits non-zero when any other variable no longer matches, or when a `TZ` or `LC_TIME`
  pinned in `[box].env` changed (that is config).
