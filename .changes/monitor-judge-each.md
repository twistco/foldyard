---
section: Fixed
bump: patch
---

- **One record the guest monitor can't make sense of no longer stops it.** A step out of a container
  that failed to be judged left every later one unjudged, and a record of a shape nobody writes
  stopped the pull at it for good. Each is now judged by itself, and such a record is stored and
  read as one that says nothing about its namespaces. Nothing is judged past records that aren't
  indexed yet: without what they say, a step out could be judged not to be one for good. If records
  still can't be indexed, `fy monitor` and `fy doctor` say so.
- **Keeping the evidence of crossings costs nothing when there is nothing to keep**: every piece was
  read several times on each pull.
