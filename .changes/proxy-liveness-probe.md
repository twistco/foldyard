---
section: Added
bump: patch
---

- **The supervisor now notices a proxy that has stopped answering, and logs it.** Each tick it
  asks the proxy for a reserved address the proxy answers itself (no upstream call, no row in the
  egress log); two misses in a row log `stopped answering` in `fy host logs` with the proxy's CPU
  use, and the answer that ends it logs how long it lasted. Before, a proxy that was running but
  stalled looked healthy (its port still accepted connections), so a box whose requests timed out
  for minutes left no trace on your computer.
