---
section: Added
bump: minor
experimental: true
---

- **`[machine] compose = "vm"` runs the compose client in the VM instead of on your computer.**
  Your stack already runs in the VM, but its compose client runs on your computer, where it parses
  compose files the box can edit; a path check stands in for a boundary there, and it can't close
  a symlink swapped in after the check. With this set, every compose call and the build of a
  project's own box image run in the VM over its ssh route, as foldyard's own pinned podman-compose,
  so a path in a compose file reads the VM, which holds nothing of yours. `fy shell` enters the
  container through the engine. Three things differ: the VM must mount the checkout, registry logins
  on your computer don't reach the VM, and `${HOME}` in a compose file is the VM's. The plan is
  docs/compose-in-vm-scope.md.
