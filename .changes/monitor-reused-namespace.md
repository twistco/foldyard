---
section: Fixed
bump: patch
---

- **The guest monitor names a container whose mount-namespace number it took over from one that
  had just died.** The kernel hands a freed number to the next namespace made, so when no relay
  snapshot fell between the old container's death and the new one's start, the join read every
  event in between as `ambiguous` — the new container's opening commands and the old one's last —
  and a hook with `who = ["container"]` dropped them without a gap (5 agent containers on one
  overnight run lost 6–49 s each). The join now reads the containers' own inits leaving and
  entering the namespace, which the store already holds, to choose between the two containers the
  snapshots named. It applies to stored history: `fy monitor --since … --until …` re-joins it.
  The store's catalogue gains these marks in place on first open (seconds on a 1 GB store).
- **A monitor hook is told about the events its `who` filter passes over because the join
  couldn't place them.** Each batch's `gaps` now carries an `ambiguous` entry, with the mount
  namespace (`mnt`), for the `ambiguous` events a hook would have wanted as `container` events,
  so a coverage check reads those times as "no evidence", not "the container did nothing". They
  are rare once the join reads the containers' inits: 31 namespaces in a busy night's whole boot,
  none outside a container being replaced.
