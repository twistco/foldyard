---
section: Added
bump: patch
---

- **`fy docs private-config`: foldyard on a project that doesn't use it.** Contributing to a
  project upstream with no `foldyard.toml`, you can keep yours untracked and hidden by
  `.git/info/exclude` in your own clone, so no commit or pull request carries it. The page gives
  the setup in order (hide the files, then review and adopt, then create the box), a
  `worktree_init` script that copies the files into each new worktree, a paragraph for the
  agent's `system_prompt`, and how to get the file back after git deletes it: your computer's
  adopted copy survives, and `fy config revert` rewrites it. The bundled `foldyard` skill now
  checks whether git tracks `foldyard.toml` before treating it as the project's.
