---
section: Fixed
bump: patch
---

- **A VM start no longer fails over Lima's half-read event.** `limactl start` reads its host agent's
  event log and, on Lima 2.3.0, can catch an event half-written and exit with
  `failed to unmarshal … as events.Event: unexpected end of JSON input` while the VM comes up fine.
  When the start fails but the host agent runs on, foldyard now reads that log itself, whole events
  only, and goes by the host agent's own verdict: running, or exiting or degraded.
