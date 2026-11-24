---
section: Fixed
bump: patch
---

- **The guest monitor no longer loses a step out of a container when the store removes its records
  early.** If the disk budget removed the segment holding a process stepping into podman's namespace
  before the relay's next snapshot, and podman had run a command since, the catalogue had dropped
  what showed the namespace was podman's: the step was judged not to be one, so hooks kept filtering
  the rest of that boot by `who`. It takes a stalled relay or a very small budget to reach.
