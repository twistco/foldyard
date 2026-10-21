---
section: Security
bump: patch
---

- **Archiving Claude transcripts copies only the checkout's own project folder.** Claude Code
  files each session in a folder named after the directory it started in, and a Claude on your
  computer resumes the folder named after its own directory. The box chooses those folder names,
  so the archive (`fy box down`, `fy nuke`, `fy worktree remove`, `fy transcripts`, the
  `transcript_sync_seconds` sweep) could place box-written sessions where `claude --resume` in a
  different project on your computer would load them. Now only the folder named after the
  checkout itself is archived (each worktree archives its own); every other entry is left in the
  bound-out dir and named — printed by `fy transcripts` and box down, logged once by the sweep.
  Sessions the box starts in a subdirectory of the checkout are left out with them. Codex
  records a session's directory inside each file rather than in a folder name, so its sessions
  are still archived unchanged; an explicit archive dir (`fy transcripts <dir>` or
  `FOLDYARD_TRANSCRIPTS_ARCHIVE`) keeps them out of `~/.codex/sessions`.
