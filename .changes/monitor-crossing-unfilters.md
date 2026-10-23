---
section: Changed
bump: minor
---

- **Once a process crosses out of a container, monitor hooks get the rest of that VM boot
  unfiltered.** From the first `crossed` event of a boot until the VM restarts, a hook's `who`
  stops filtering. That covers the `vm` and `podman` events a hook without `who` skips, and
  whatever a hook's own `who` leaves out. What an escaped process does next is labelled by the
  VM's or podman's namespace, and missing it is worse than the extra volume (on a busy night,
  about twice the events). Each batch this affects says so in a new `unfiltered` field, naming the
  crossing and the container it came from. `fy monitor` and `fy doctor` say when this boot has
  had a crossing. Set `keep_who_after_crossing = true` on a hook to keep its filter; it still gets
  the `crossed` events themselves. The first read after upgrading looks for crossings in the store
  (about 13 s on a 330 MB store).
