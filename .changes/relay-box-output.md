---
section: Security
bump: patch
---

- **What your stack, your builds and your box print no longer reaches your terminal as it is.** The
  compose files, Dockerfiles, images and scripts behind `fy up`, `fy ps`, `fy logs`, `fy down`,
  `fy mode`, `fy box up`, `fy reclaim` and `fy worktree add`/`remove` are the box's to write, and
  control sequences in their output could write to your clipboard, erase lines or fake a ✓. Their
  output now has those sequences stripped, as it already had with `[machine] compose = "vm"`, so it
  is no longer in colour. `fy shell`, `fy box shell` and `fy box exec` still hand over your
  terminal. Compose now runs with `PYTHONUNBUFFERED=1` in its environment, so `fy logs` keeps up as
  lines arrive; a compose file that names it sees that value.
