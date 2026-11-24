---
section: Fixed
bump: patch
---

- **The guest monitor never names the wrong container for a crossing it judged late, and says when
  it can't judge one.** If judging kept failing until the store had removed the records around a
  step out of a container, the step was named after whichever container held the reused namespace
  number later, or after none. And while judging failed nothing was said on your computer (hooks
  were told): now the log, the notification, `fy monitor` and `fy doctor` say a process may have
  crossed, after a minute.
