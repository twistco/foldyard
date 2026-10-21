---
section: Added
bump: minor
---

- **Experimental, with the guest monitor: each event names the process that started it.**
  `fy monitor --json` and hook payloads gain `ppid`, `parent` and `parent_args` (the parent's id,
  binary and arguments) and `exec_id`/`parent_exec_id` (the links to rebuild the whole process
  tree from `exec` events), all taken from what Tetragon already reports. Without them a consumer
  could only attribute by time, and a container's own daemon was blamed on whatever a session was
  doing: under BashArena, a firewall daemon's reload running `iptables -F` scored the same against
  an honest run as an attack. The fields are additive (the schema stays `fy.monitor.events/1`) and
  empty on events that lack them; `fy monitor`'s own output is unchanged.
