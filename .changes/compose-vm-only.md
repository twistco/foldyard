---
section: Changed
bump: minor
---

- **Compose always runs in the VM now, never on your computer.** The compose files are editable from
  the box, so the client that reads them runs where the stack does: an `env_file:`, a build context
  or an `include:` reads the VM, which holds nothing of yours but the checkout. This was
  `[machine] compose = "vm"`, experimental since 0.9.0, and it replaces the check that refused
  compose files naming paths outside the checkout, which could only narrow that channel. Your
  stack's images are built in the VM too. `fy shell` enters the container through the engine. What
  changes for a project that didn't set `compose = "vm"`:
  - The VM must mount the checkout. A VM `fy machine ensure` created from it does; one created from
    another checkout refuses, naming `fy machine recreate`. A worktree's stack needs the VM's
    worktrees root too, which a VM from before worktree support lacks (`fy machine ensure` warns of
    it, naming the same fix). Nothing needs recreating otherwise, VM or box.
  - `${HOME}`, `~` and `$USER` in a compose file are the VM's, not your computer's. A bind mount of
    a path on your computer found nothing in the VM before, and still doesn't.
  - Registry logins have no route into the VM: `FY_REGISTRY_AUTH_FILE` in `[project] compose_env`
    reached only the client on your computer, and does nothing now (`fy doctor` says so). Pull a
    private image into the VM once yourself (`fy docs stack`, Private images).
  - `[machine] compose = "vm"` still works and changes nothing. `compose = "host"` and
    `MACHINE_COMPOSE=host` are ignored, and `fy doctor`, `fy config features` and the stack verbs
    say so: delete the line.
  - Your own recipes that run `"$ENGINE" compose` after `eval "$(fy shellenv)"` are still a client
    on your computer.
- **A `[ports]` key must be a port's name: upper case with `PORT` as a word in it.** `APP_PORT`,
  `SUPABASE_DB_PORT` and `PORT` work as before. Any other key is ignored and `fy doctor` names it,
  so a project with one sees it reported and its value no longer set. `[ports]` keys are exported by
  `fy shellenv`, which recipes eval before running `fy up`, git or anything else, and passed into
  the box: `PYTHONPATH`, `BASH_ENV` or `IN_DEVBOX` there would have run what the box wrote on your
  computer. A port's name foldyard reads itself (`FY_PROXY_PORT`, `DOCKER_*`, …) is ignored too, and
  a port can no longer override one of foldyard's own variables.
