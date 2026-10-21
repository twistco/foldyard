---
section: Changed
bump: patch
---

- **The VM's boot scripts now say "VM firewall" and "host" instead of "wall" and "Mac".** Their
  comments, messages, the guest's `fy-wall` unit descriptions and one nft rule comment changed;
  no rule, path or unit name did. Every byte of those scripts is in the boot provisioning a Lima
  VM records, and the firewall script is recorded with the firewall off too (its `uninstall`
  runs then). So every Lima VM, with or without `[machine] firewall`, is refused as stale once:
  `fy up` stops until you run `fy machine stop && fy up`.
