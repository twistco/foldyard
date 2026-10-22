---
section: Added
bump: minor
experimental: true
---

- **Hook batches say what they can't vouch for.** Every batch now carries a `gaps` list (usually
  empty) naming the stretches of time where evidence is missing or incomplete: the store removed
  events before the hook read them, events were lost on the way from the VM, the relay restarted,
  a batch was skipped, or events were labelled without the record that would confirm their
  container. Mark those times "no evidence", not "nothing happened". Each event also carries
  `mnt`, its mount namespace number. Both are new keys in the same `fy.monitor.events/1` schema;
  a batch can now hold gaps alone (`events` empty, `first`/`last` null), and file-sink lines
  that carry them still pass `fy monitor check`.
- **`fy monitor --since/--until` reads any stretch the store still holds.** Give an ISO time (UTC
  unless it has an offset) or a span back from now (`90s`, `15m`, `2h`, `1d`). Events are
  labelled as they were when recorded, and missing evidence in the range is listed.
- **The monitor's store keeps hours of activity instead of minutes.** Older events are
  gzip-compressed (about 30 times smaller) and the store keeps 1 GB on your computer by default
  — about 13 million events, where it used to keep 160 MB of uncompressed records, two minutes of
  a busy stack start. Set `max_mb` in `~/.foldyard/<project>/monitor-store.toml` (64 MB to 64 GB);
  it is never read from `foldyard.toml`, so the box can't shorten what is kept about it.
  `fy monitor` and `fy doctor` show how much is kept and how far back. Old files are converted in
  place; anything copying `logs/monitor*.jsonl` should now also take `monitor*.jsonl.gz`.
