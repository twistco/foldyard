---
section: Added
bump: minor
---

- **`fy ls` lists every foldyard project on your computer, from anywhere.** For each one it shows
  the VM (running or not, its CPUs and memory, the disk it takes), the host supervisor, the boxes up
  and the agents running in them. Below that it lists what needs attention, each with the command
  that fixes it: a VM running without its supervisor, a supervisor that is failing, wedged or still
  on the foldyard you had before upgrading, a version floor or recommendation you're below,
  `foldyard.toml` changed since it was adopted, a registered worktree whose directory is gone. It
  also lists what's left over, such as VMs foldyard made that no project uses any more. It only
  reads: a stopped VM is reported, never started, and nothing is removed. `--json` for scripts.
