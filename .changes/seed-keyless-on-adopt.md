---
section: Fixed
bump: patch
---

- **A worktree gets main's keyless `claude`/`codex` modes however its config is first adopted.**
  They were copied only when `fy worktree add` itself adopted the config, so a worktree adopted
  later — at the `fy box up` or `fy up` prompt after declining at `add`, with
  `fy config adopt`, or from the TUI — came up with both off and its box warned. The copy now
  happens at the first adoption, wherever it is, and only then: a later adoption copies nothing,
  and a mode the worktree already sets is left as it is.
