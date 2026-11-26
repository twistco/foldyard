---
section: Security
bump: patch
---

- **What `foldyard.toml` says at a consent prompt or in a report is printed as text.** A
  `[proxy] recommend` entry's `why`, an `[[inject]]` row's `label`, a `[[secret]]` row's `label` and
  `how`, a `[[require]]` row's `reason` and `message`, and a `[machine] firewall_allow` entry's
  `why` now show any terminal control characters in them as visible escapes, as does everything
  `fy config widenings` prints. The file can be written from the box, so these could otherwise write
  to your clipboard, or erase the host you are being asked to allow or the line of the report that
  names a widening. A recommended host carrying such characters is no longer offered:
  `fy config widenings` lists it as ignored. An `[[inject]]` or `[[require]]` row whose names (a
  switch, a host, a header, a level) hold such characters is refused, like any other malformed row,
  with the value shown as text.
