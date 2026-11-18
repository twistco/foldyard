---
section: Security
bump: patch
---

- **The foldyard wheel a box installs is no longer staged through a symlink the box planted.** When
  your computer runs foldyard from an editable checkout, `fy box up` builds a wheel into
  `.devbox-foldyard/` in your checkout, which the box can write. A box that swapped that directory
  for a symlink had your computer delete every `*.whl` where it pointed and build a wheel there. The
  wheel is now built in a private temporary directory and copied in without following a symlink,
  and `fy box up` refuses with the path to remove, as it already did for the proxy CA. Published
  installs never staged a wheel and are unaffected.
