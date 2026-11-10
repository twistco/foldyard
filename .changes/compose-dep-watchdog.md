---
section: Fixed
bump: patch
---

- **`fy up` no longer hangs forever when a service's dependency was never created.**
  podman-compose starts a service only once each `depends_on` dependency meets its condition, and
  retries that check with no limit, so a dependency whose create failed (a bad device, a removal
  the engine refused) left `fy up`, or a `fy mode` reconcile of a running stack, waiting
  indefinitely with no hint why. foldyard now watches the engine while compose `up` runs: once
  compose has created or started a container (so not while it pulls or builds), a dependency with
  no container for 90 seconds while nothing else changes stops compose and fails, naming the
  missing container and the way out (fix the create error compose printed, then
  `fy down && fy up`). Starting posture services on a stopped stack isn't watched yet.
