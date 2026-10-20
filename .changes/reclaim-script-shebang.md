---
section: Fixed
bump: patch
---

- **A `[reclaim] script` runs by its own shebang, and a failed one is reported.** foldyard ran it
  as `sh <script>`, so a bash script ran under the box's dash and died at its first bash-ism
  before any of its sweeps ran; the exit code was ignored, so a single stderr line was the only
  sign. An executable script now runs by its shebang (a file without the executable bit still
  runs under `sh`), and a non-zero exit prints a `⚠` line. It still doesn't stop `fy up`.
  If your script `eval`s `fy shellenv`, remove that: shellenv's output is bash and is meant for
  recipes.
