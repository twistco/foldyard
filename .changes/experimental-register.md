---
section: Added
bump: patch
---

- **Settings foldyard isn't ready to stand behind are now marked *(experimental)* in the
  configuration docs, and `fy doctor` lists the ones your config turns on.** Each says which of
  two things it means: its config or output may still change, or it is settled but unproven in
  day-to-day use. `[machine] runtime = "gvisor"` and `[machine] vmtype = "krunkit"` are the first
  two; they work as before. A setting stops being experimental in a release whose changelog says
  so.
