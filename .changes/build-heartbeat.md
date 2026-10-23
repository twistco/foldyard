---
section: Changed
bump: patch
---

- **`fy up` and `fy build` print a line every 30 s while podman images build.** The build's own
  output goes to the build log, not the terminal, so a long cold build used to be minutes of
  silence that looked like a hang. Each line names the images still building, how many are done
  and the time so far, and says whether the log grew since the last line: "no new output" is how
  a build that really is stuck shows itself.
