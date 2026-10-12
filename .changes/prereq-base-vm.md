---
section: Added
bump: minor
---

- **`fy prereq` checks what your computer needs and prints how to install it.** Lima (and its
  version), podman, a hypervisor, and on Linux QEMU, `/dev/kvm` and a `systemd --user` session,
  plus disk, memory and foldyard's own egress proxy. Each missing piece comes with the install
  command for the package manager you have; foldyard never runs it. It needs no project, so
  `uvx foldyard prereq` works before foldyard is installed, and it exits non-zero until
  everything required is there.
- **New projects' VMs are clones of a base VM: seconds instead of minutes, and almost nothing
  downloaded.** `fy prereq --fetch` (or `fy machine base bake`) bakes a project-less base VM once:
  Lima's podman template with nothing mounted, booted, the generic box image built in it. After
  that, `fy up`/`fy box up` create a new project's VM as a `limactl clone` of it with the
  project's own sizing and mounts, and `fy box up` re-tags the box image it carries in instead of
  building one. From an empty directory to a walled box took 26 s on macOS. Existing VMs are
  never re-cloned; a base out of date with Lima's template is not used, and one over 30 days old
  is flagged. `FOLDYARD_BASE_VM=0` turns cloning off. `fy docs base-vm` has the rules.
