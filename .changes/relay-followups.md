---
section: Security
bump: patch
---

- **What `fy box exec` prints to your terminal now has its control sequences stripped.** The git
  hooks run it during a `git commit` on your computer, where in-box tools (a linter, a formatter, a
  package manager) print what the checkout leads them to. Escape sequences in that output could
  write to your clipboard or rewrite lines already on your screen. This protects a hook you trust
  that runs tools you don't: the box can also write `.git/hooks`, `lefthook.yml` and `.git/config`,
  so a hook it wrote can still print whatever it likes. Only output going to your terminal is
  cleaned. Piped or redirected (`fy box exec 'pg_dump' > dump.sql`), it passes byte for byte as
  before, and the exit code still decides whether the commit goes ahead. So a hook that captures the
  output and prints it itself (`out=$(fy box exec 'pnpm lint') || { echo "$out"; exit 1; }`) still
  prints it raw: let `fy box exec` write to the terminal instead, as lefthook and a plain
  `fy box exec` line in a hook do.
