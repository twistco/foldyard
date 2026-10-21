---
section: Security
bump: patch
---

- **Archiving transcripts never follows a link out of the checkout.** The bound-out transcript
  dirs (`.devbox-claude/projects`, `.devbox-codex/sessions`) live in the checkout, which the box
  can write, and the host copied them into your history (`~/.claude/projects`, `~/.codex/sessions`)
  with rsync — which follows a symlink in the source path. A box could therefore make `fy box down`,
  `fy nuke`, `fy worktree remove`, `fy transcripts` or the `transcript_sync_seconds` sweep copy any
  directory you can read on your computer into that history. The copy is now foldyard's own walk,
  which opens every directory on the way and every entry inside without following a link: a link
  on the way to the transcript dir refuses the archive and names the path; one inside it is
  skipped, as are pipes and other special files. Otherwise it behaves as before (additive, newer
  copies win, mtimes kept), writes new archive files private to you, skips a single file over 4 GiB
  and stops copying before the archive's disk falls under 1 GiB free. rsync is no longer needed on
  your computer. One change for `transcript_sync_seconds`: a file the sweep cannot copy (unreadable,
  too large, no room) is now reported once, like any other failure, instead of passing silently.
