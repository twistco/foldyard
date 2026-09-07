# ADR-0021 — Per-kernel git index split: a runtime-installed shim gives box-side git its own index file

- **Status:** Accepted (2026-07-11) — implemented (`src/foldyard/assets/box/git-index-shim.sh`,
  `box.py _git_shim_step()`, `config.box_git_index_split()`); behaviours below verified
  empirically in scratch repos (git 2.47).
- **Sources:** the corruption lore formerly in the Tangible root `CLAUDE.md`; the options
  discussion this distils (mount-vs-sync, 2026-07-11).

## Context

On the two-kernel machine backends (podman/lima on macOS) the checkout is shared between the
host and the VM over virtiofs. Git's index update protocol — take `index.lock` with
`O_CREAT|O_EXCL`, write, `rename()` over `index` — assumes one kernel's VFS; virtiofs does not
guarantee that atomicity across kernels. Both kernels *write* the index: box-side git ops
obviously, but also host-side background pollers — `git status` (and porcelain `diff`)
**opportunistically rewrite the index** to refresh its stat cache even when nothing changed, so
merely having the repo open in VS Code or a git GUI makes that side a writer. The observed
failure was `.git/index` coming back **empty**: `git status` showing every tracked file as a
staged deletion (recoverable with a plain `git reset`, but scary and recurring). Notably,
GUI *auto-fetch* was never the index racer — `fetch` writes refs/objects only.

Host-side hygiene (closing the GUI, disabling autorefresh) can't be guaranteed — any tool that
merely *displays* repo state is a writer. The `native` Linux backend has one kernel and no such
race, so the fix belongs at the box layer, not in the universal contract.

## Decision

Box-side git gets its **own index file per checkout** (`<gitdir>/index-box`); objects and refs
stay shared, so commits are visible on both sides instantly, but the two kernels never write
the same index file again.

Delivery is a **PATH shim installed at runtime by the box bootstrap** — the first monitored
`run_step` heredocs the packaged `assets/box/git-index-shim.sh` to `/usr/local/bin/git`. No
Dockerfile involvement: it works identically for the packaged default box image and any
consumer-supplied image (ADR-0014), and it is degradation-safe by construction — a failed
install is a reported ✗ step, and absence of the shim just means the previous shared-index
behaviour. Opt out with `[box] git_index_split = false`; per-call escape `FY_GIT_SHIM_OFF=1`.

Shim mechanics (each point traces to a verified failure mode):

- **Per-invocation gitdir resolution** (`rev-parse --absolute-git-dir`, honouring
  `-C`/`--git-dir`; linked worktrees resolve to `<main>/.git/worktrees/<name>/`). A fixed
  exported path is dangerous: git run in a *different* repo with a foreign `GIT_INDEX_FILE`
  fails loudly on reads but **writes succeed silently**, injecting foreign entries.
- **First-touch seeding** from the shared index. A missing index file reads as *empty* — every
  tracked file would show as a staged deletion, the exact scare being prevented.
- **Deliberate `GIT_INDEX_FILE` is respected** (git's own temp-index protocols: `stash`,
  `commit -a` hooks), but the shim's *own* injection is marked (`FY_GIT_SHIM_INDEX`), so a
  child git that merely inherited it re-resolves for its own repo instead of reusing the
  parent repo's index.
- **`clone`/`init` are skip-listed**: `git clone` writes the new repo's checkout through an
  inherited `GIT_INDEX_FILE`, leaving the fresh clone looking broken.

## Consequences

- The index race is gone structurally: the box's everyday git (the frequent writer) can no
  longer collide with host-side polling (the unattended writer), regardless of what tools
  anyone leaves open on the host.
- **Staging state is per-side, with deterministic illusions.** After box-side commits or
  branch switches, a *host-side* `git status` shows `MM` pairs (staged old content + unstaged)
  or staged-`D` + `??` for files the box added — the same *class* of scare as the old
  corruption, but scoped, harmless, and fixed by the same `git reset` (mixed). Symmetrically,
  the box resets `index-box` after host-side git activity.
- **The one destructive edge: committing on a stale side.** A plain `git commit` there commits
  the stale index — verified to silently create a commit *reverting* the other side's work —
  and `commit -a` only self-heals for modifications, not for files the other side added. The
  ritual is unconditional: reset before committing when the other side has done git work.
  (This edge existed before in racy form; the split makes it deterministic and documentable.)
- **Hooks bypass the shim** — git prepends its exec-path to hooks' PATH, so in-hook `git` is
  the real binary inheriting `GIT_INDEX_FILE`. Correct for same-repo hook work (lint-staged
  sees the box's real staged state); a hook driving git in a *different* repo must scrub git
  env itself (`env -u GIT_INDEX_FILE`) — the same footgun class as stock git's temp-index
  commits. Tools linking libgit2/gitoxide (not spawning the CLI) also bypass; rare in
  practice.
- **Refs remain shared** (`refs/*`, `packed-refs`) under the same cross-kernel lockfile
  weakness — much rarer (deliberate ops, not polling), failing as a stale `.lock` file rather
  than an emptied index. Host-side GUI auto-fetch off shrinks it; if it ever bites, consider
  `gc.auto=0` in the shared config with maintenance run host-side.
- Stray `index-box` files are derived state — safe to delete anytime (the shim re-seeds).
- Follow-up: an `fy verify` assertion that in-box `git` resolves to the shim, so a regression
  fails loudly instead of silently re-arming the race.

## Rejected alternatives

- **Host-side writer hygiene alone** — discipline, not a guarantee: `status`-shaped index
  writes come from anything that displays repo state, including tools users forget are open.
  Still worth doing (it shrinks the residual refs surface) but not sufficient.
- **`GIT_INDEX_FILE` via shellenv env export** — misses every process that doesn't descend
  from a shellenv'd shell (above all the in-box VS Code server's git integration — exactly the
  unattended-poller class that causes the race), and a fixed exported path has the verified
  silent cross-repo write hazard.
- **Baking the shim into box images** — per-image opt-in that misses consumer images
  (ADR-0014 makes the image the consumer's) and costs rebuild churn; the runtime bootstrap
  covers every image with the same code path.
- **VM-native clone + git-native sync** (host pulls from the VM over `podman machine ssh`) —
  the principled "deliberate sync" model, but it inverts ADR-0001's durability story
  (uncommitted work would live on a VM disk that `machine recreate`/`nuke` treat as
  disposable) and requires wide surgery (worktree lifecycle, compose mounts, `MAIN_REPO`-keyed
  paths). Multi-agent parallelism, its other draw, is already served by ADR-0004 worktrees.
  Remains a *perf-gated* future option — only worth revisiting if measured hot-path costs on
  the mount (source watch, `git status`) justify it.
- **File-sync daemons (mutagen/unison/rsync)** — sync `.git` and the corruption returns
  asynchronously; exclude it and each side needs its own git, i.e. the VM-native model with a
  weaker conflict engine, plus a daemon on the ADR-0006/0016 supervisor surface and conflict
  states living outside git.
