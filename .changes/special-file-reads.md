---
section: Security
bump: patch
---

- **A file the box swaps for a pipe or a device no longer stops `fy host`'s background loop, and
  foldyard reads only regular files from the checkout.** The box can write the checkout, so it could
  put a named pipe where foldyard reads a file (a worktree's `.git` or `HEAD`, `foldyard.toml`, a
  compose file, the Dockerfile), or one it keeps writing to, or a link to `/dev/zero`. Opening a
  pipe waits for a writer that never comes, and the others never end. In `fy host`'s background loop
  that stopped everything after it, so a mode with a time limit stayed on past it. foldyard now
  reads only regular files there, up to 16 MiB (a link to `/dev/null` still reads as empty), and
  says which file it couldn't read and why. A compose file that isn't a regular file is refused,
  with that reason, before compose runs on your computer (a `.env` that isn't one is left to
  compose, which skips it). When a mode change updates a running stack, compose gets at most 10
  minutes, and running out is reported as a skipped update, never a done one. `fy host` makes that
  update too when a time limit running out also turns off a switch that can't work without the
  expired one.
- **A `foldyard.toml` or `foldyard.local.toml` that can't be read is never adopted as missing.** The
  diff, the launch prompt, the adopt dialog in `fy tui`, `fy config status`, `fy doctor` and the
  `fy host` log say which file isn't read and why. Where adopting would drop what the adopted copy
  holds (the narrowings in a `foldyard.local.toml`), adopting isn't offered until it's fixed.
