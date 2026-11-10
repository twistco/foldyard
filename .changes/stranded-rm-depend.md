---
section: Fixed
bump: patch
---

- **`fy up` no longer hangs after a VM restart when one stopped service `depends_on` another
  stopped one.** Before `up`, foldyard removes stopped containers that podman-compose would remove
  and never re-create (dependents of a service whose config changed). It removed them in one
  `podman rm`, which podman runs in parallel, so a container that another one `--requires` was
  refused in any order, and the refusal went unseen. compose then skipped creating it and waited
  forever for it to turn healthy: `fy up` hung with the dependent's container missing. The removal
  now passes `--depend`, which takes dependents first. A container from outside the project that
  depends on one of them would go too, with nothing to recreate it, so `fy up` refuses before
  removing anything and names it instead. If the engine still refuses, `fy up` stops before
  compose starts and names the way out (`fy down && fy up`). The sweep of containers whose service left the
  config now reports a refused removal too, instead of leaving the orphan without a word.
