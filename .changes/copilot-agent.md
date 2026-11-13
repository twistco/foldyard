---
section: Added
bump: minor
---

- **GitHub Copilot CLI runs in the box: `fy copilot`, plus `[copilot]` and `fy agent`.** Declaring
  a `[copilot]` table installs the CLI when the box is created (its installer's static release
  tarball) and mounts a `$COPILOT_HOME` volume, so a login, settings and sessions survive box
  recreation and `fy nuke`. `fy copilot` launches it with `--allow-all` and `COPILOT_ALLOW_ALL=true`
  — the rootless box is the safety boundary, the same posture as `fy claude` and `fy codex` — and
  `COPILOT_AUTO_UPDATE=false`, so the box runs the build its bootstrap installed. `[copilot]`
  `system_prompt` is the same key as the other agents' with a different delivery: the CLI has no
  `--append-system-prompt`, so `fy copilot` writes it to
  `$COPILOT_HOME/instructions/foldyard.instructions.md`, a personal instruction source every
  session loads. `fy init` scaffolds the block (and `--with copilot` writes it active), and
  `fy agent start|resume` handles Copilot sessions by the CLI's own session id.
