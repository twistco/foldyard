---
section: Changed
bump: patch
---

- **`fy claude` starts a fresh box straight at the prompt.** Claude Code used to ask two
  questions first: whether to trust the folder (where Enter picks "No, exit") and whether to
  accept bypass-permissions mode, which `fy claude` always launches in. It now answers both in the
  box's own Claude config, for the checkout you launch from. Trusting the folder is safe because
  the box is the boundary: the repo's hooks and MCP servers reach no further than the agent
  itself. The theme now starts on `auto`, which follows your terminal's light or dark background.
  All three are defaults: a theme you pick with `/theme` stays.
