---
section: Removed
bump: minor
---

- **The podman backend (`[machine] backend = "podman"`) is gone: foldyard runs on Lima only
  (ADR-0036).** Deprecated since 0.7.0. A config that still names it, or `MACHINE_BACKEND=podman`,
  is refused before anything touches a VM: `fy up`, `fy box up` and `fy machine ensure|recreate`
  stop and say how to move, `fy doctor` shows it red and `fy ls` shows the VM as `retired`. Your
  podman machine and its volumes are left as they are, and `fy config diff|adopt` and
  `fy worktree list` keep working, so a project's own migration can still run. Without one, set
  `backend = "lima"` (or drop the key), `fy config adopt`, and `fy up` creates a new, empty Lima VM.
  `backend = "native"` gets the same refusal instead of falling back to podman.
- **`fy init` no longer writes `backend = "lima"`**, and shows `vmtype` as a comment instead: unset,
  each computer gets the best hypervisor it has (vz on macOS, qemu on Linux and WSL2). A config that
  says `backend = "lima"` keeps working.
