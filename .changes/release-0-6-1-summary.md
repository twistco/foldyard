---
section: Summary
bump: patch
---

Monitor hooks no longer drop container events under load, and the monitor keeps far more history:
`fy monitor --since/--until` reads it back, and each hook batch names the stretches it can't vouch
for. On the Lima backend, a read-only to-box and a box-written from-box now sit beside the checkout:
`fy monitor hook add NAME --to-box` gives the box a signed event file it can read but not change,
and a new box's transcripts leave the checkout. After upgrading, on the Lima backend run
`fy machine stop && fy up` once (the VM's mounts changed; a running one is refused until it
restarts), then `fy box down && fy box up`. On the `podman` backend, `fy host restart` is enough.
