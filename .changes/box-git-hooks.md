---
section: Added
bump: minor
---

- **`[box] git_hooks = ".githooks"` runs the checkout's hooks in the box and not on your computer.**
  foldyard sets `core.hooksPath` in the box's own git config, for this checkout and its worktrees
  only. Other repos in the box keep their own hooks. Git on your computer never sees the setting,
  since the box can rewrite those hooks. Before this, getting the same result took a long
  `[box].bootstrap` line that only worked on newly created boxes. `fy box up` now applies the
  setting to a box that's already running too, and clearing it takes it off again. See
  `fy docs git`.
