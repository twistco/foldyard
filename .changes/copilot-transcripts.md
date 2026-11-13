---
section: Added
bump: minor
---

- **Box Copilot sessions are archived on the host, into a Copilot home of foldyard's own.**
  `fy transcripts`, `fy box down`, `fy nuke` and `fy worktree remove` copy
  `$COPILOT_HOME/session-state/<uuid>/` out of the box, and an explicit archive gets a
  `<dir>/copilot-sessions/` child. By default they land in
  `~/.foldyard/<project>/transcripts/copilot/session-state`, never `~/.copilot` on your computer —
  one project's box cannot write into another project's history, or into the sessions a plain
  `copilot --resume` on the host lists for every project. Only UUID-named directories are copied;
  the CLI's `session-store.db` and lock dirs are housekeeping, not sessions. The run ends with the
  recipe that reads them back: `COPILOT_HOME=~/.foldyard/<project>/transcripts/copilot copilot
  --resume`. `DEVBOX_COPILOT_TRANSCRIPTS` points the sweep at a different source, and
  `[copilot] transcript_sync_seconds` keeps the periodic sweep on.
