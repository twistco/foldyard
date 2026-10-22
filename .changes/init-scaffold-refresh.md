---
section: Changed
bump: patch
---

- **`fy init` writes a config that matches what fy does today.** Step 2 said to uncomment an agent
  and run `fy box up` again, which does nothing on a running box: an agent is installed when the
  box is created, so it now says `fy config adopt`, then `fy box down && fy box up`. The scaffold
  now offers `[proxy] no_proxy` (the step-3 prompt sends the agent to services by container name,
  which hang under the proxy without it), `[project] worktree_init`, and a commented GitHub block:
  an App row and your own `gh` token as a `github-user` emergency switch. `runtime = "gvisor"` is
  marked experimental (a test now holds both scaffolds to the experimental register),
  `host_firewall` warns that committing it refuses colleagues who aren't on Linux, and `keyless`
  says it belongs in `foldyard.local.toml` on a team. In the local example, the private injector
  gets a `box_env` placeholder, loses a wrong "add it to the allowlist" step and warns that a local
  `[[inject]]` replaces the shared rows; `vmtype`, `[host] notifications` and `[claude.settings]`
  join it. `fy init` ends by pointing at `fy prereq`. Existing `foldyard.toml` files are unchanged.
