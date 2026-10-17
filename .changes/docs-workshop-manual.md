---
section: Added
bump: patch
---

- **Five new pages in `fy docs`: `workshop`, `stack`, `git`, `github-app` and `gcp`.** Running a
  workshop (what attendees set up beforehand on macOS, Linux and Windows via WSL2, and the order
  on the day); bringing a compose stack in, including podman-compose's differences from docker
  compose; git when the box and your computer share a checkout; setting up a GitHub App for
  `[[inject]] kind = "github-app"`; and Google Cloud access through the metadata emulator. The
  manual pages now carry diagrams (on GitHub), and the quickstart's agent step is corrected: on a
  running box, adopt the config and run `fy box down && fy box up` — `fy box up` alone only warns.
