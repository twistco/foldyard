---
section: Fixed
bump: patch
---

- **Node's built-in `fetch` now goes through the proxy in image builds, and a build that fails
  because a tool skipped the proxy now says so.** Node's `fetch` ignores `HTTPS_PROXY` unless
  `NODE_USE_ENV_PROXY=1` is set, and Node 24's corepack (0.35 and later) uses it. So
  `corepack prepare` in a `node:24` image connected directly, the VM firewall refused it, and the
  proxy logged nothing. foldyard now sets `NODE_USE_ENV_PROXY=1` in every build step (no `ARG`
  needed in the Dockerfile; a Dockerfile `ENV` for it still wins), as it already did in the box.
  When a build fails, foldyard also lists the connections the VM firewall refused while it ran,
  by IP and port, with the likely causes. Before, a failure like this left no trace. **Existing
  VMs are refused as stale until you run `fy machine stop && fy up`**, which installs the change.
