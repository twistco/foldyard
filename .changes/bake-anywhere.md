---
section: Fixed
bump: patch
---

- **`fy machine base bake` and `fy prereq --fetch` run from any directory.** Both are meant to
  work outside a project, but they refused to start from a directory whose name isn't a plain
  name, such as `_temp`, `My Projects`, a folder under iCloud's `Mobile Documents`, or `/`. The
  error talked about "the checkout's directory name" and `[project].name` to someone in no project
  at all. The bake no longer reads a project name.
