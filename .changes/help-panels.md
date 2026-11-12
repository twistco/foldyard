---
section: Changed
bump: patch
---

- **`fy --help` groups the verbs**: start here, the stack, the box and its agents, posture and
  egress, machines and projects, testing, and last the verbs of experimental features (today
  `fy monitor`). Every verb is still where it was on the command line.
- **Inline code in `fy` help no longer sits on a black background**, which showed as black slabs on
  a light terminal theme. It keeps its colour on whatever background your terminal has. Each verb's
  line in a help listing is now one short, capitalised sentence; the detail moved to
  `fy <verb> --help`.
