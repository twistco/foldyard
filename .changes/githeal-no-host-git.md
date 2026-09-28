---
section: Security
bump: minor
---

- **Your computer runs no git at all to heal the shared index.** 0.3.2 hardened the supervisor's
  git calls against a box-written `.git/config`; now there are none. The box's git shim builds the
  healed index and leaves it next to the shared one, and the supervisor only installs it — under
  git's own lock, and only if neither the index nor HEAD changed since, so nothing staged on your
  computer is ever lost. Nothing to migrate: a box on the new shim is picked up by `fy box up`;
  until then a stale host-side `git status` is fixed by `git reset`, as before the heal existed.
  (Follow-up to GHSA-j5mq-v7p4-qw2j; ADR-0021.)
