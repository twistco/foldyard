---
section: Added
bump: minor
experimental: true
---

- **`[machine] compose = "vm"` runs the compose client in the VM instead of on your computer.**
  Your stack already runs in the VM, but its compose client runs on your computer, where it parses
  compose files the box can edit; a path check stands in for a boundary there. With this set, the
  client runs in the VM over its ssh route, as foldyard's own pinned podman-compose, so a path in a
  compose file reads the VM, which holds nothing of yours. It arrives in steps: so far the calls
  that only read (`ps`, the service list, the config hash `fy state` compares) move, while `fy up`,
  the builds, `fy logs` and `fy shell` still run on your computer under the path check. The plan
  is docs/compose-in-vm-scope.md.
