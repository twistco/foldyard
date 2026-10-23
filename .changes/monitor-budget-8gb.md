---
section: Changed
bump: patch
---

- **The guest monitor's store now keeps 8 GB on your computer by default, up from 1 GB.** A real
  project's monitor wrote 21–61 MB an hour during busy batches, so 1 GB held only 15–35 hours of
  them and evidence rolled off before anyone read it. 8 GB is about 106 million events: 5–16
  days of those batches, about six weeks at a steady 30 events a second. The budget is per
  project, so each monitored project can take up to 8 GB. A `max_mb` in
  `~/.foldyard/<project>/monitor-store.toml` still wins (64 MB to 64 GB).
