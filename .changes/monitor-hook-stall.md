---
section: Fixed
bump: patch
---

- **A monitor hook no longer stops delivering, without an error, after the upgrade to 0.6.1.** On
  its first round the new supervisor could read the store while the pull was sealing the older
  foldyard's live file, miss the hook's place in it, and report "the store rotated past its
  position" when nothing had been removed. It then sent the hook back to the oldest record —
  behind events it had already delivered — where it found nothing new to send and never moved
  again, while `fy monitor hook` and `fy doctor` showed ✓. A hook's place is now declared lost
  only when the store proves its events gone, the older live file is indexed before it is
  sealed, and a hook behind its own cursor moves up to it. **Upgrade and run `fy host restart`:
  a hook stuck this way resumes right after the last event it delivered**, with no duplicates.
  `fy monitor hook` and `fy doctor` now warn when a hook's cursor has not moved for ten minutes
  while events kept arriving.
