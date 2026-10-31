---
section: Security
bump: patch
---

- **foldyard's own child processes no longer import code from the checkout.** Several ran as
  `python -m <foldyard module>` in a checkout's directory, inherited from the `fy` command that
  started them or set outright. Plain `-m` puts that directory first on Python's import path, and
  the box can write the checkout, so a `foldyard/` package planted at its root would have run on
  your computer in their place. The affected children: the config-hash check `fy up` runs, the
  `fy box up` that `fy worktree add` starts, the launch gate's re-run, the host-firewall probe, and
  the credential minters the proxy runs, which a planted package would have run beside the
  credential they were handed. All of them now run as `python -P -m`, which leaves the working
  directory off the import path and changes nothing else.
