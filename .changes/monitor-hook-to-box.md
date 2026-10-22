---
section: Added
bump: patch
---

- **`fy monitor hook add NAME --to-box` puts the monitor's signed event file where the box can
  read it and can't change it.** The file is `~/.foldyard/<project>/to-box/monitor/NAME.jsonl`,
  which every box sees at `/run/foldyard/to-box/monitor/NAME.jsonl`, read-only. A file in the
  checkout could be edited, cut or replaced by the box (the signature found that, but couldn't
  stop it). Each line is still signed, so a copy that leaves your computer can still be checked
  with `fy monitor check`. It needs the lima backend on `vz` or `qemu`; elsewhere it says why and
  points at `--file`, which works as before. A `--file` inside the checkout, the worktrees root
  or the from-box now gets a warning that the box can write there.
