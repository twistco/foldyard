---
section: Added
bump: minor
---

- **`fy monitor` shows what the guest monitor recorded, labelled with each event's container and
  worktree.** A root relay in the VM signs every event. Your computer pulls them every few seconds
  over the VM's ssh and keeps only lines that verify, counting any that went missing or were
  altered (`fy doctor`'s `monitor events` row). Containers are identified from the kernel's own
  records, not from the container id Tetragon guesses, which a container's creator can forge.
  Names and worktrees are what the engine reports, and are shown as claims.
