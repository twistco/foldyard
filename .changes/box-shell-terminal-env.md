---
section: Fixed
bump: patch
---

- **`fy box shell` now tells the box what terminal you're using.** The engine's `exec -t` set
  `TERM=xterm` and nothing else, so tools in the box saw a 16-colour terminal: Claude Code mapped
  its theme onto your terminal's basic palette, where the selected option in a menu could all but
  vanish on a light background. Each attach now passes your computer's `TERM`, `COLORTERM`,
  `TERM_PROGRAM`, `TERM_PROGRAM_VERSION` and `COLORFGBG` (whichever are set), and falls back to
  `xterm-256color` when the box has no terminfo for your `TERM` (Ghostty's `xterm-ghostty`,
  kitty's `xterm-kitty`).
