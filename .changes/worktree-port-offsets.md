---
section: Fixed
bump: patch
---

- **Two worktrees no longer share ports.** A worktree's port offset was a hash of its name into 89
  slots, so two names could land on one (28% odds by eight worktrees): their stacks wanted the
  same ports, and their egress proxies one port, where the supervisor took each proxy for the
  other's leftover and killed it in turn. `fy box up` failed with the proxy `exited (rc 0)`, and
  while both boxes were up either could reach the other's proxy and credentials. Your computer
  now records an offset for each worktree it knows, keeping the hashed one while no other worktree
  has it, so most worktrees keep their ports. The second of two that shared one moves; if its box
  is up, `fy box up` says to recreate it. And should two daemons still want one port (a pin, or
  `WT_OFFSET`), the supervisor runs one and `fy host logs` names the other.
