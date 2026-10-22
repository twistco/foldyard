---
section: Changed
bump: patch
---

- **`fy tui`'s Network Log now lists every host in the retained egress log, with refusals you
  haven't answered on top.** It used to group only the last 400 requests, so an agent's own API
  traffic pushed every other host out within minutes — including the refused ones you had to
  grant. Each host's tallies now count its whole history (the live log and its rotated backups);
  its rows are the newest 50, plus any older refusal, then a line saying how many are earlier.
  A host goes back to its place by recency once you grant or decline it.
