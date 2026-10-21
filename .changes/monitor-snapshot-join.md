---
section: Fixed
bump: patch
---

- **Monitor hooks no longer silently drop container events under load.** A hook matched events to
  their containers using only the records near its place in the store. When events arrived fast —
  a few hundred a second while stacks start, or even 30–40 a second from ten idle containers —
  the two records that name the containers were further apart than that, so the events between
  were labelled `unattributed`, a `who = ["container"]` hook left them out, and nothing said so.
  In one measured run a hook lost 5% of its container events, in holes of 10–60 seconds; in
  stack-start storms, whole tool calls. Every stretch of the store is now matched against the
  records around it wherever they are, so a hook gets what `fy monitor` over the whole store
  shows. Events dropped before this fix can't be recovered: the store had already removed them.
  `fy monitor` had the same flaw and is fixed too. Restart the host supervisor
  (`fy host restart`) to pick it up.
