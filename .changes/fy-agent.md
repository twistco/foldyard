---
section: Added
bump: minor
---

- **`fy agent start|ls|attach|stop`: agents in tmux sessions that outlive their terminal.**
  `fy agent start` runs `fy claude` (or `fy codex`) in a detached session in the box, from your
  computer or the box. `fy agent ls` lists every local project's sessions without starting any
  VM. `fy agent attach <id>` reattaches from anywhere on your computer, and
  `fy agent clean --older-than 7d` removes sessions that exited longer ago than that (never a
  running one). For Claude the id is Claude's own session id, so the id a transcript viewer such
  as claude-code-log shows is the one to attach with. foldyard runs its own tmux server, which by
  default keeps an agent's escape sequences and clipboard writes off your terminal (a default,
  not a boundary: the box can turn it off). `fy claude` is unchanged. See `fy docs agents`.
- **The packaged box image now includes tmux.** Its fingerprint changes, so the next `fy box up`
  rebuilds the image, and `fy prereq --fetch` re-bakes the base VM. On your own `[box].image`, add
  `packages = ["tmux"]` to use `fy agent`.
- **`fy box down` names the agent sessions still running in the box before removing it**, and
  asks first on a terminal (`--yes` skips the question). The TUI's stop-box confirmation lists
  them too.
