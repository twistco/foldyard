---
section: Fixed
bump: patch
---

- **`fy up`, `fy box up` and `fy box build` wait for the host supervisor's daemons, and refuse
  when one can't run.** A launch used to start the supervisor and carry on: right after it
  replaced the supervisor (an upgraded foldyard, say), the first build could reach the egress
  proxy before it was listening and fail with apt's "connection refused". Each launch now waits
  until the supervisor reports every daemon it needs as running. A daemon blocked on a spawn gate
  (a missing `host.env` secret, a foreign process on its port) or crash-looping stops the launch
  with its reason. Before, the launch carried on and failed somewhere later and less clearly.
- **A worktree's launch is served before its box exists.** The supervisor only ran daemons for
  worktrees whose box was already up, so with one worktree's box running and main's not, a second
  worktree's `fy box up` built with no proxy listening on main's port, which every image build
  goes through. A launch now tells the supervisor which worktree it is bringing up.
