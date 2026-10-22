---
section: Deprecated
bump: patch
---

- **The podman backend (`[machine] backend = "podman"`) is deprecated and will be removed in a
  future release.** Each project gets its own podman machine, but only one of them can run at a
  time, so two projects can't be up at once; and it can't carry the VM firewall. Nothing built on Lima since
  reaches it either: no read-only to-box, no from-box (its transcripts stay in the checkout), no
  guest monitor. Set `backend = "lima"` to move: the next `fy up` creates a new VM, so images
  are rebuilt there and the podman machine's volumes, agent logins included, don't come along.
