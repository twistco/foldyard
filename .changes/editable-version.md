---
section: Fixed
bump: patch
---

- **An install from a foldyard checkout reports the version it is actually running.** An
  editable install (`uv tool install --editable`) recorded its version once, when it was
  installed, while the code kept following the checkout — so after pulling a new release,
  `fy --version` and `fy doctor` still named the old number, the version window judged the old
  number, and `fy box up` warned that the box had a newer foldyard than your computer when both
  ran the same code. foldyard now reads the version from that checkout's `pyproject.toml`, so a
  pull is enough; no reinstall is needed just to correct the number.
