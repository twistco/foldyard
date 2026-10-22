---
section: Changed
bump: patch
---

- **On the lima backend, the VM shares two directories of foldyard's own, and transcripts leave
  the checkout.** Beside the checkout, the VM now mounts `~/.foldyard/<project>/from-box/`, which
  the box writes, and `~/.foldyard/<project>/to-box/`, which only your computer writes: the VM and
  every box can read it (in the box, at `/run/foldyard/to-box`), and not even root in the VM can
  change it, because the hypervisor's share refuses the write. That holds on `vz` (virtiofs) and
  `qemu` (9p), so foldyard now pins Lima's `mountType` to match the hypervisor and mounts no to-box
  on any other (`krunkit`). A new box binds Claude's and Codex's transcripts into its checkout's
  slot in the from-box instead of `.devbox-claude/` / `.devbox-codex/` in the checkout, so
  `git clean -fdx` and grep no longer see them and `fy worktree remove` archives the slot, then
  deletes it. For this release the transcript sync reads both places; the next drops the old one.
  `fy verify` checks the to-box with a real write, and `fy doctor` has a `to-box` row.
  **Every Lima VM needs one `fy machine stop && fy up`** (its mount set changed; until then
  foldyard refuses the running VM, and nothing is lost: containers and volumes stay), **then
  `fy box down && fy box up`** to move a box's transcripts out of the checkout (`fy box up` warns
  until you do). **On the podman backend nothing about the VM changes** — its mounts are fixed
  when the machine is created, and no podman provider enforces read-only outside the guest — so
  there is no restart to do, and boxes keep binding transcripts into the checkout.
  `FOLDYARD_STATE_DIR` must now point outside the checkout and the worktrees root on Lima
  ([ADR-0033](https://github.com/twistco/foldyard/blob/main/docs/adrs/0033-a-to-box-and-a-from-box-beside-the-checkout.md)).
