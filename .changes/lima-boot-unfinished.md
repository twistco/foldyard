---
section: Fixed
bump: patch
---

- **A VM whose boot never finished now says where it stopped.** Lima runs its own provisioning
  before foldyard's boot script — on a VM created from the template, that's its podman install
  (`dnf`, over the Fedora mirrors). A stalled mirror made `limactl start` give up after 10
  minutes, and the next `fy up` then said the VM "did not apply its boot provisioning" and
  pointed at a boot log that didn't exist yet, because foldyard's script never started. Both now
  say the VM is up but Lima's boot scripts haven't finished, show how to see what's still running
  and where the guest console log is, and give the restart. A VM whose base was baked with
  `fy machine base bake` skips that install altogether.
