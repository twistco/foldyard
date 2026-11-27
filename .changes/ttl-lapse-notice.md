---
section: Fixed
bump: patch
---

- **Leaving a level removes the services only it wanted, and `fy up` does too.** Switching gcp off
  (or letting `gcp=user` run out) left the metadata emulator running while the stack was up, because
  re-rendering kept every running profile as if you'd started it. A running service whose profiles
  are all ones a level adds (`metadata`, or `cloudsql` from storage=staging) now goes once no level
  enables them, in the same run, after the services that pointed at it have been recreated without
  that setting, and never when that run fails. Your own profiles, such as `data` workers, are
  untouched. A Cloud SQL proxy you start by hand in the `cloudsql` profile goes at the next mode
  change or `fy up` that doesn't enable it.
- **When an emergency level runs out under a running stack, you're told how to bring the stack in
  line.** The supervisor stopped the level's token service and switched it off, but a running stack
  kept what the level set up, and nothing said so. You now get a notification naming the level that
  ran out (and any switch lowered with it) and saying that `fy up` brings a running stack in line,
  and `fy doctor` says so on your computer and in the box for as long as the stack still differs
  from the mode. The supervisor still doesn't run compose itself: that could hold up its other work
  long enough to get it restarted.
- **A mode change made just after a level ran out reconciles the stack for that level too.**
  `fy mode` and `fy tui` compared the new mode with one where the level had already run out, so a
  change made before the supervisor noticed the expiry didn't count the expired level as part of the
  change. A switch that can't work without the expired level is lowered with it, and `fy mode` says
  so, unless the same command turns the level back on.
- **Compose profiles from two plugins both apply.** When two switches' levels each enabled a compose
  profile, only the last plugin's took effect, so the other level's services weren't started and the
  stack could be re-rendered without them.
- **`fy state` in a worktree checks that worktree's stack.** It compared main's containers with the
  worktree's mode, so it could call a worktree's stack in line, or down, while main's was the one it
  looked at.
