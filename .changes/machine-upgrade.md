---
section: Added
bump: minor
---

- **`fy machine upgrade` moves a project's VM to a newer Fedora in place.** A VM keeps the Fedora it
  was created with, and `fy machine recreate` would throw away its images, volumes and agent logins.
  This upgrades the disk instead: it stops the containers, runs the upgrade as root at one boot (a
  boot script foldyard records and drops again, as with its other root setup), restarts on the new
  kernel, checks every volume, image and container against a snapshot taken first, and starts what
  ran before. It never goes past the Fedora your Lima's own podman template uses, which is the check
  that Lima is made for it; that is also the default, so with Lima 2.3.0 an existing VM goes to
  Fedora 45. A real project's VM went from Fedora 44 to 45 in about two and a half minutes.
