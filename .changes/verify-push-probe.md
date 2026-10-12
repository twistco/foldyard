---
section: Fixed
bump: patch
---

- **`fy verify`'s push check works in a walled box, and on a public origin.** Since 0.3.2 its
  `git` ran with the compose client's environment, which carries no proxy: in a box behind the
  VM firewall git went direct, was refused, and every origin read "git remote UNREACHABLE … push
  refusal UNPROVEN". It now runs with the box's own environment, as every other box check does.
  And the probe is a dry-run push (`git push --dry-run`, which sends nothing) instead of
  `git ls-remote`: a read proves nothing about pushing — a public origin answers it with no
  credential, which made the row FAIL on every public repo — while the push handshake needs write
  access on any repo.
