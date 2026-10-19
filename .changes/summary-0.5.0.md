---
section: Summary
bump: minor
---

Every VM needs one `fy machine stop && fy up` after upgrading: the VM's boot setup changed (the
proxy settings it gives build steps and stack containers), and until it restarts foldyard refuses
a running Lima VM as stale. No config edits are needed. Outside a foldyard project, project verbs
now refuse instead of running as a project named after the directory. New: `fy prereq` checks
what your computer needs, and `fy prereq --fetch` bakes a base VM that new projects clone in
seconds; `[box].packages` bakes system packages into the box image once; and `fy changelog`
prints what changed since the foldyard your `foldyard.toml` declares.
