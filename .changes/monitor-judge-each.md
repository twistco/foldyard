---
section: Fixed
bump: patch
---

- **One record the guest monitor can't make sense of no longer stops it.** A step out of a container
  that failed to be judged left every later one unjudged, and a record of a shape nobody writes
  stopped the pull at it for good. Each is now judged by itself, and such a record is stored and
  read as one that says nothing about its namespaces.
- **Keeping the evidence of crossings costs nothing when there is nothing to keep**: every piece was
  read several times on each pull.
