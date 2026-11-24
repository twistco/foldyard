---
section: Added
bump: minor
---

- **The guest monitor keeps what it recorded around a step out of a container, past the store's
  budget.** Once such a crossing's records left the store, only its time and container were left:
  which program it was, what led up to it and what it did next were gone. Now the minute either side
  is copied aside (up to 256 MB, `evidence_mb` in the host-only `monitor-store.toml`, `0` for none).
  Each VM boot's first crossings are the ones kept; when it is full a new one waits and you are
  told, and the oldest goes only when the new one's records would otherwise be lost.
  `fy monitor evidence` lists them, prints one and removes one. While they can't be copied, the
  store is held past its budget: by a tenth, or up to twice the budget where that leaves 50 GB free
  on the disk.
