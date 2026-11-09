---
section: Summary
bump: patch
---

Two fixes for the VM firewall, a fixed engine socket path for compose services on lima, and,
experimental and off by default, a compose client that runs in the VM. Stack services can now reach
each other by name, and `fy verify` no longer fails a checkout whose origin is an SSH URL. After
upgrading, restart each lima VM once (`fy machine stop && fy up`): its boot provisioning changed,
and `fy up` refuses the running VM until it restarts. Then recreate the stack's containers
(`fy down && fy up`) so they pick up the new `NO_PROXY`.
