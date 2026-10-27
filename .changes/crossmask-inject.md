---
section: Added
bump: minor
---

- **A worktree added while a dev box runs is now masked in that box straight away, without
  recreating it.** Each box mounts the dependency dirs (`[box].shadow_volumes`) of every other
  checkout read-only, so an install there fails instead of writing onto your computer's disk. Those
  mounts are set when the box is created, so a worktree added later used to stay unmasked in every
  running box until you recreated it, and `fy worktree add` could only name the boxes. Now
  `fy worktree add` adds the masks to each running box ("masked '<name>' in <box> without
  recreating it"), and `fy box up` on a running box adds any it lacks, for example after the box or
  the VM restarted (which drops them). foldyard does this from your computer, through the VM's ssh
  as the VM's own user, with no root: it is the same read-only mount a create would make, the box's
  root can't remove it, and a symlink anywhere on the path is refused, never followed. It can't do
  this for a box under the gVisor posture, a rootful engine in the VM, or a VM kernel older than
  Linux 5.12. There, and for a dependency dir that is a symlink, it names the box and the reason,
  and recreating the box masks it, as before. `fy doctor`'s `box shadowing` row and the
  already-up `fy box up` now read a running box's real mounts, so they count a mask added this
  way.
