---
section: Fixed
bump: patch
---

- **`fy up` no longer hangs forever when a service's dependency was never created.**
  podman-compose starts a service only once each `depends_on` dependency meets its condition, and
  retries that check with no limit, so a dependency whose create failed (a bad device, a removal
  the engine refused) left `fy up`, or a `fy mode` reconcile, waiting indefinitely with no hint
  why. foldyard now watches the engine while compose `up` runs: a dependency with no container for
  90 seconds while nothing else changes stops compose and fails, naming the missing container and
  the way out (fix the create error compose printed, then `fy down && fy up`).
