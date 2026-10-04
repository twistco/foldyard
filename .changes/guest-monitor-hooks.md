---
section: Added
bump: minor
---

- **Guest monitor hooks: send batches of attributed events to your own program or endpoint.**
  Configure them in `~/.foldyard/<project>/monitor-hooks.toml` on your computer, never in
  `foldyard.toml`, because anything in the box can write the checkout. A command gets each batch
  as JSON on stdin with a minimal environment, so `host.env` secrets are not passed. A URL gets it
  as a POST. Events are sent once their attribution is settled. Delivery is at least once,
  retried with backoff, and a batch is skipped and counted after five failures.
