---
section: Fixed
bump: patch
---

- **On Linux and WSL2 (Lima on QEMU), a box now sees what your computer writes into the to-box.**
  Lima mounts a read-only 9p share with a cache that never re-checks the files, on the assumption
  that nothing changes them, but your computer keeps writing the to-box, so a box could keep
  reading an old copy indefinitely. foldyard now pins that mount's cache to `mmap`, as Lima already
  does for the writable mounts. Writes are still refused outside the guest, exactly as before.
  **Restart each Linux or WSL2 VM once:** `fy up` refuses a running one with the old setting until
  `fy machine stop && fy up` (containers and volumes are kept). Nothing changes on macOS.
