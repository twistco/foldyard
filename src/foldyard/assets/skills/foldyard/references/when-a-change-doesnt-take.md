# "I changed it and nothing happened"

Every entry below is a place where the thing you edited is **not** the thing that's running. They
share one root cause: state that the box can write is never authoritative for what the host does,
and containers freeze parts of their config at creation. Work down the table before assuming a bug.

| you edited | why it didn't take | what makes it take |
| --- | --- | --- |
| `foldyard.toml` / `foldyard.local.toml` | the host reconciles from the copy it **adopted**, outside the mount — your edit is inert by design | the human answers the adopt/revert/ignore prompt at the next `fy up`/`fy host restart`, or runs `fy config adopt`. `fy config status` / `fy config diff` show where you are |
| a block you deleted from `foldyard.toml` | `foldyard.local.toml` wins the merge and may still declare it (that file is gitignored, so it's invisible in `git status`) | `fy config widenings` names the file behind each one; a local `disabled = true` is how one person opts out of a block the team keeps |
| a compose file, inside a worktree | each worktree's stack reads its own checkout's compose files — the edit reaches only that worktree's stack | bring that stack up: `WORKTREE=<name> fy up` (or `fy up` from inside the worktree) |
| a compose file, anywhere | a running container keeps the command + env it was CREATED with | `fy up` (recreates on a config change) — a `restart` is not enough |
| an env var in the stack's env file | same: baked at container creation, and an `env_file`'s contents aren't part of the config `fy up` compares | `fy down` then `fy up` |
| an `[[inject]]` row's `box_env`, `[box].env`, an agent table — with the box already running | the box's environment is fixed when it's CREATED; `fy box up` on a running box keeps it (and `fy box up` / `fy mode` name each variable that moved) | a new box: the human runs `fy box down && fy box up` on their computer — from in here, `fy box down` stops the box you're in |
| the mode (`fy mode …`) | setting it is host-only; from the box it refuses | ask the human, then re-check `fy mode` |
| an allowlist grant | grants live in the host's home, never in the repo | `fy allow add <host>` on the host |
| a code file, expecting hot reload | the watcher may not see host-side writes across the VM's shared filesystem — but **edits made in the box do** propagate | make the edit in the box (you're already there); if a host-side edit was the trigger, touch the file from here |
| a bundled skill (this one) | `.claude/skills/` holds a *copy*; upgrading foldyard doesn't refresh it | `fy skill install <name> --force` copies the installed version over it. `fy docs` always matches the install |

## Diagnosing, in order

1. `fy config status` — is the config the host runs the one you edited?
2. `fy state` — desired vs observed, per tier (files, daemons, capability, stack, box). It exits
   non-zero on drift, so it's the fastest "is anything out of sync" check.
3. `fy doctor` — one line per check with its fix; the row that's warning usually is the answer.
4. `fy ps` / `fy logs <svc>` — is the container even the one you think? Check when it was created.

## The general rule

If a change would let the **box** widen what the **host** does — its credentials, its egress, what
it decrypts — assume it needs a host-side act, and say which one. That's not an obstacle to route
around; it's the property that makes it safe to run an agent here with permissions skipped.
