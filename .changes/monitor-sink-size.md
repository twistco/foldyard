---
section: Added
bump: minor
---

- **A monitor hook's file can be compressed and rotated.**
  `fy monitor hook add NAME --to-box --gzip --max-mb 512` compresses each signed line (some 15–20
  times smaller) and moves a full file aside, each piece still verifiable with `fy monitor check`.
  The file had no limit: one reached 25 GB. And `fy monitor check` reads a line at a time, where it
  took some 50 GB of memory for that file.
