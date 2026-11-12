---
section: Added
bump: minor
---

- **`fy box recreate` removes the dev box and creates it again in one step, and the TUI's `r` does
  the same for the highlighted workspace.** It replaces `fy box down && fy box up`, which every
  "recreate the box" hint used to spell out (those hints now name the new verb; `fy box restart` is
  an alias). A box takes its env, mounts, foldyard install and image only when it is created, which
  is why this recreates rather than restarts the container. The gates, the secret prompts and the
  supervisor's readiness all run before the old box is removed, so a recreate that can't go ahead
  leaves it running. Agents still running in it are named first, as `down` does (`--yes` skips the
  question). From inside a box it refuses: it would remove the box it runs in.
