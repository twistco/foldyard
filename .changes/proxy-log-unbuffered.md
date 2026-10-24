---
section: Fixed
bump: patch
---

- **The egress proxy's lines in `fy host logs` carry the time they were written, each on its own
  line.** mitmdump's output was block-buffered into the supervisor's log, which stamps each line as
  it reads it: lines arrived in batches up to an hour late, and a batch ending mid-line ran the next
  `[host]` line into it. The proxy now runs unbuffered, so its launch env changes and the
  supervisor restarts it once, when the supervisor first runs the upgraded foldyard.
