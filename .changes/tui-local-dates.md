---
section: Fixed
bump: patch
---

- **The TUI's Network Log, GCP Tokens and grant expiries show your local time, with the date on any
  row that isn't from today.** They showed the log's UTC time of day alone, so a request from
  yesterday read like one from this afternoon, and every time was off by your UTC offset.
