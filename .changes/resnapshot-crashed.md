---
section: Fixed
bump: patch
---

- **A service that crashed while a credential was failing now comes back when it recovers.**
  `[resnapshot_on_capability]` only recreated services that were still running, so one that
  fetches its secrets at startup and exited because that fetch failed stayed down after the fix
  (for example `just gcp-elevate`), even though the notification said the switch had recovered.
  It took a manual `fy up`. A listed service whose container exited with an error is now
  recreated as well. One that was stopped (exit 0, or by Ctrl-C or `stop`) or never started is
  still left alone. List such services under the switch, as before.
