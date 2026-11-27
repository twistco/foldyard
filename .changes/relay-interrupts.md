---
section: Fixed
bump: patch
---

- **Ctrl-C on `fy logs` ends it without the compose client's traceback.** The interrupt now reaches
  foldyard alone, which stops relaying and then stops the compose client and what it started. The
  same happens when the terminal closes or foldyard is stopped with `kill` (SIGHUP, SIGTERM,
  SIGQUIT).
- **`fy logs` starts straight away and shows each line as it arrives.** With the compose client in
  the VM (the experimental `[machine] compose = "vm"`, the only way since this release) it used to
  wait six seconds before starting, then delivered lines in batches.
