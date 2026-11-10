---
section: Summary
bump: patch
---

Two fixes for `fy up` hanging forever, both seen on a Lima VM restarted after a config change. A
stopped container that another one depends on is now removed safely before compose runs, and if a
dependency still never gets a container, foldyard stops compose and says which one is missing,
instead of letting podman-compose wait forever. After upgrading, run `fy host restart` so the
supervisor's `fy mode` reconciles use the fix too.
