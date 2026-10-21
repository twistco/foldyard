---
section: Security
bump: patch
---

- **Box Codex sessions are no longer archived into `~/.codex/sessions` on your computer.** Codex
  records a session's working directory inside the session file, which the host can't check, so
  a session the box wrote could claim another project's directory and be offered by
  `codex resume` when you worked in that project. `fy transcripts`, `fy box down`, `fy nuke`,
  `fy worktree remove` and the `transcript_sync_seconds` sweep now archive them into a Codex
  home of foldyard's own, one per project: `~/.foldyard/<project>/transcripts/codex`. There is no
  setting that puts them back in `~/.codex`. To resume one, run
  `CODEX_HOME=~/.foldyard/<project>/transcripts/codex codex resume` from the checkout after
  logging in there once; `fy transcripts` prints the line. An explicit archive dir
  (`fy transcripts <dir>` or `FOLDYARD_TRANSCRIPTS_ARCHIVE`) still files them under
  `codex-sessions/`. Sessions earlier versions archived into `~/.codex/sessions` stay where they
  are — foldyard never moves or deletes them; each also sits under the same name in the
  checkout's `.devbox-codex/sessions` unless that was cleaned, and the next archive copies it to
  the new place too.
