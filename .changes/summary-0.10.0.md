---
section: Summary
bump: minor
---

The VM firewall can now open one TCP port to one address, for a client that can't go through
the egress proxy. cloud-sql-proxy, for one, dials its database instance directly, so it couldn't
work behind `firewall = true` until now. The firewall's refusals now carry a time, and `fy verify`
lists them, so a client that fails behind the firewall shows why. After upgrading, restart each
walled lima VM once (`fy machine stop && fy up`): its boot provisioning changed.
