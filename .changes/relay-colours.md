---
section: Changed
bump: minor
---

- **Image builds write to a log, and your terminal shows only where it is unless the build fails.**
  `fy up`, `fy build` and the dev box image builds of `fy box build`, `fy box up` and
  `fy box recreate` each keep one log per kind of build in `~/.foldyard/<project>/`, replaced by the
  next build, so a warm build no longer scrolls hundreds of lines past the ones that matter. A long
  build says every 30 seconds that it is still going. A failed one shows the log's last 25 lines and
  its path. If the disk fills part way, the rest is shown on your terminal and fy says so. Each line
  in the log has a timestamp, and the box's escape sequences are removed, so `cat` on it later is
  safe. `--verbose` (`-v`) shows the output as it comes, as before.
- **What the stack prints, `fy logs` above all, has colour again, chosen by foldyard.** Since 0.15.0
  the stack's output reaches your terminal with every escape sequence removed, because coloured text
  would let the box draw a line that looks like one of foldyard's own. foldyard now adds its own
  colour from what each line says: a colour per service prefix that stays the same from run to run,
  and `ERROR`, `FATAL` and `CRITICAL` in red, `WARN` and `WARNING` in yellow. It does this only on a
  terminal, and never into a file or a pipe.
- **`fy logs` shows when each line was written,** dimmed and in your time, in front of its service:
  the tails of several services arrive mixed together, and the time is what puts them in order. Into
  a file or a pipe, the line keeps compose's full timestamp. `fy logs --no-timestamps` leaves it
  out.
