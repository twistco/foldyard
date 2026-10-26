---
section: Added
bump: minor
---

- **`fy init --with claude,vscode` writes those blocks already uncommented.** An agent and the
  VS Code server volume are set up when the box is created, so turning one on after `fy box up`
  meant an adopt and a box recreate. Named at `init`, the first `fy box up` creates the box with
  them, and step 2 of the scaffold's header says to switch the agent on rather than uncomment it.
  The names are `claude`, `codex` and `vscode` (repeat the flag or comma-separate them): the
  blocks that work exactly as scaffolded. An active `[codex]` carries the scaffold's agent prompt
  instead of the commented `"…"` placeholder. Without `--with` the scaffold is unchanged.
