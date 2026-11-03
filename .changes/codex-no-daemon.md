---
section: Fixed
bump: patch
---

- **`fy codex` works with Codex 0.166.** Codex now starts a shared background server by default
  and manages it with `ps`, which the packaged box image doesn't have, so `codex` exited at once
  ("failed to invoke ps for pid-managed app server"). `fy codex` now runs it without that server
  (`-c features.daemon_auto_start=false`), which older Codex versions accept too. With the server,
  an agent's work would also run outside its terminal. `features.daemon_auto_start = true` in
  `[codex.config]` turns it back on.
