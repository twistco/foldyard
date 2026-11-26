---
section: Security
bump: patch
---

- **`fy claude` run on your computer now warns you not to resume a box's session there.** A box's
  Claude sessions are archived into your computer's Claude history, so `claude --resume`,
  `--continue` and `/resume` there offer them. A resumed session is text the box wrote, and that
  Claude would act on it as its own history, with your access.
