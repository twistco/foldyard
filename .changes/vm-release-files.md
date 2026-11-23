---
section: Fixed
bump: minor
---

- **The VM lets go of shared files nothing is using, so your Mac no longer runs out of open files.**
  macOS keeps a file open for every file a VM remembers on a shared folder, and a VM with memory to
  spare never forgot one: four projects' VMs once held 474,000 of the 491,520 a Mac allows, mostly
  worktrees' `node_modules` and `.venv`, and everything else on it failed with "Too many open files
  in system". foldyard now turns on the kernel's own expiry (`fuse.inval_wq`, 60 s): in testing, 50,000
  held files fell to 77 within 75 s. Files in use are kept. `[machine] release_files` sets the
  period, `0` turns it off. It is set at boot, so each VM needs one `fy machine stop && fy up` after
  the upgrade, and `fy up` says so.
