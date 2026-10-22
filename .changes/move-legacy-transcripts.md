---
section: Changed
bump: patch
---

- **On Lima, creating a box now moves the old transcripts out of the checkout.** 0.6.1 started
  binding new boxes' transcripts into foldyard's from-box, but left the old `.devbox-claude/` and
  `.devbox-codex/` in the checkout with a note that they could be deleted: still matched by
  `grep -r`, and missing from `claude --resume` inside the new box. The first `fy box up` that
  creates a box now copies them into that box's slot and into the archive on your computer, then
  deletes them from the checkout, never following a link and only when both copies are complete.
  Anything it can't copy whole stays, with a warning, and the next new box tries again. With
  `DEVBOX_TRANSCRIPTS` set, the old place is only archived, as before.
