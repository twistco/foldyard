---
section: Fixed
bump: patch
---

- **`fy worktree add` now adopts the new worktree's config, and its box gets main's keyless
  `claude`/`codex` modes.** Copying those modes from main read the worktree's adopted config,
  which a fresh worktree didn't have yet, so it never copied anything: the worktree's box came up
  with both off while main had them on, and `fy box up` then asked for the first adoption. A
  config byte-identical to the one adopted for main is now adopted without a prompt (it grants
  nothing new) and the modes are copied after it. A config that differs is shown as a diff
  against main and adopted only if you answer the prompt; with no terminal, or if you decline, the
  worktree is still made and the message says how to adopt it later. `-y` still only answers the
  question about registering an existing worktree, and never adopts a config.
