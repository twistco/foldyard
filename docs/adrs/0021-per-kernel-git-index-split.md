# ADR-0021 — Per-kernel git index split: a runtime-installed shim gives box-side git its own index file

- **Status:** Accepted (2026-07-11) — implemented (`src/foldyard/assets/box/git-index-shim.sh`,
  `box.py _git_shim_step()`, `config.box_git_index_split()`); behaviours below verified
  empirically in scratch repos (git 2.47).
- **Sources:** the corruption lore formerly in the Tangible root `CLAUDE.md`; the options
  discussion this distils (mount-vs-sync, 2026-07-11).
- **Amended 2026-09-08:** a periodic worktree snapshot recorded under *Rejected alternatives* —
  considered for the destructive-git residuals below, not taken; its event-triggered slice ships
  in `worktree remove`.
- **Amended 2026-09-18:** `.git/config` added to the shared-file residuals below — foldyard
  itself was its most frequent writer (issue #6).
- **Amended 2026-09-24:** the "podman/lima on macOS" scope below is now every host. The
  one-kernel `native` backend was retired ([ADR-0027](./0027-always-a-vm-native-backend-retired.md)),
  and Linux and WSL2 hosts run the same Lima VM (validated in CI; the repo is shared over 9p
  there rather than virtiofs), so the checkout is always shared between two kernels and the
  split applies on every backend.
- **Amended 2026-09-27:** the host's heal of the SHARED index runs no git — the box proposes the
  healed index, the host only renames it into place (Consequences, "The shared index's heal").
  Cross-kernel visibility measured to support it, and the direction that does NOT hold recorded.
- **Amended 2026-09-29:** refs are not safe either — the box can read a branch the host just
  moved as older or absent, and a box commit then rewound or re-rooted the host's branch. Ref moves
  are now checked under git's own ref lock, and the shared index's heal is sealed to the box's ref
  move (Consequences, "Ref moves are checked under git's ref lock" and "The shared index's heal";
  the live record is the §9 diary behind this amendment).

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
- **`clone`/`init`/`worktree` are skip-listed**: `git clone` writes the new repo's checkout
  through an inherited `GIT_INDEX_FILE`, leaving the fresh clone looking broken. `git worktree
  add` does the same (skip-listed 2026-09-22) — and the index it hijacks is the CALLING
  checkout's index-box, so both broke at once: phantom `MM` pairs in the caller (a commit there
  reverts the difference; a later `switch` silently carries those files over as local edits),
  no index at all in the new worktree.

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
  practice. *(2026-09-29: no longer — a repo hook's `git` goes through the shim again; see "Ref
  moves are checked under git's ref lock".)*
- **Refs remain shared** (`refs/*`, `packed-refs`) under the same cross-kernel lockfile
  weakness — much rarer (deliberate ops, not polling), failing as a stale `.lock` file rather
  than an emptied index. Host-side GUI auto-fetch off shrinks it; if it ever bites, consider
  `gc.auto=0` in the shared config with maintenance run host-side. *(2026-09-29: it bit, and not
  as a stale lock — see the next point.)*
- **Ref moves are checked under git's ref lock** (2026-09-29). A host git moves a branch by
  replacing its file, and the box reads a REPLACED name as absent or old (22 ms–~1 s on Lima vz,
  up to ~5 s on podman machine's libkrun — above). Git reads a missing loose ref as "the packed
  value, else unborn", and its own under-lock re-read is just as stale, so on a loaded libkrun VM
  a box commit parented on an old commit or made a ROOT commit, and moved the host's branch there;
  a box `git reset -q` rewound 7 host commits. What holds now:
  - **Refresh first.** A create-style lookup always asks the server again, and `link(2)` does one
    on its target before failing: `ln -dT / <name>` refreshes a name and creates nothing. The shim
    refreshes HEAD, its branch, `packed-refs` and `config` before and after its heal.
  - **Judge under the lock.** Git has no config-declared hooks before 2.54, so the shim passes a
    per-call `-c core.hooksPath=<libexec>/foldyard-git-hooks` — a directory of symlinks to itself,
    installed beside it by the box bootstrap (`FY_GIT_SHIM_INSTALL_HOOKS=1 git`) — and delegates
    every hook to the repo's own. In its `reference-transaction` hook, at `prepared`, git holds
    the ref lock, so the host can't move the ref and a refreshed read is the truth. A move is
    refused when git read a value that isn't that truth; a move of the checked-out branch is also
    held to the HEAD the command started from (advanced by its own moves), whatever git sends as
    the old value — `reset` reads its target, rewrites the index (seconds on a loaded VM), and
    only then reads the value it replaces, so a host commit in between passed git's own
    compare-and-swap. A move with no expected value must land on that HEAD or ahead of it.
  - **Ask again under the lock.** Every check before git runs is a hint: a host `pull --rebase`
    that started after the shim's "no operation in progress" check had its rebase broken by a box
    commit. The hook re-asks for a host operation's state (not one the box started, `fy-box-op`).
  - **An unborn read of a branch with history is refused** before git runs (its reflog is
    appended in place, never replaced, so it is reliable over the mount): shim and git both read
    "unborn" after a host rebase finished, and every check agreed with the root commit that
    followed. An orphan branch has no reflog.
  - **Discovery.** A HEAD the host just rewrote can hide the repository ("not a git repository"):
    on a failed discovery the shim refreshes `.git` up the tree and asks once more. A nested
    repo's `.git` that reads absent makes discovery succeed in the OUTER repo instead: a command
    that can write, started below the repo git found, re-asks each `.git` on the way up.
  - **Repo hooks.** A repo hook's `git` (lefthook's two dozen calls a commit) goes through the
    shim again — a directory holding only `git`, first on the hook's PATH — so it reads through the
    refresh; in the same repo it takes a light path (refresh, then real git). Git hands the
    `core.hooksPath` override down to hooks, so a hook's read-only git drops exactly that entry:
    lefthook's auto-sync asked `rev-parse --git-path hooks`, got the shim's directory and wrote
    its hooks into it (through the symlinks over the shim itself when writable). The directory is
    root-owned in the box; handing a hook to the shim's own directory is refused (it looped).
  - **Attribution.** Only a move this command's own transaction committed counts as the box's
    (FY_MOVED): "HEAD changed while it ran" claimed a host commit after a refused box commit, and
    the agent's next commit reverted it.

  Costs: a box commit takes two hook round trips; a refusal is "nothing was changed; run it
  again", and under a host commit every second most of a slow command's attempts are refused
  (measured: 2 of 20 box resets landed on a loaded libkrun VM, 0 host commits lost). Git before
  2.31 has no `reference-transaction` hook: the shim then only refreshes. With git ≥ 2.54 in the
  box, config-declared hooks (`hook.<name>.event`) could replace the `core.hooksPath` override
  and everything it has to hide. The shim must stay bash 3.2-compatible: macOS's `/usr/bin/env
  bash` is 3.2, and the hermetic suite runs it there.
- **`.git/config` is shared too**, and here foldyard WAS the racer: `shellenv`/`resolve` wrote
  `core.fileMode=false` unconditionally — every recipe and engine verb, from both kernels — and
  `git config` rewrites the file (lock → rename) even for an unchanged value. A lost update left
  a consumer's config as the 25-byte `[core] fileMode = false`: remote, upstreams and git's own
  init keys gone, and git degrading silently (refs still resolve) until `fy verify` blamed
  egress for the missing origin (issue #6, 2026-09-11). Now read-first (`stack.pin_filemode`):
  one write per checkout. The pin stays in the FILE rather than moving to the shim's env,
  because the writer it protects is host-side git outside any shellenv — a GUI client's own
  bundled binary (Fork ships its git) is exactly the process that would otherwise see phantom
  mode changes; the env-injection objection below applies unchanged. `fy doctor`'s
  `shared git config` row names the signature (`repositoryformatversion`/`bare` missing) with
  the recovery, and `verify` distinguishes "no origin" from "origin unreachable".
- **The shared index's heal: the box proposes, the host only renames** (2026-09-27). The split
  makes the host the shared index's only writer, so a box HEAD move leaves `.git/index` describing
  the old HEAD until something heals it. That heal first ran host-side git on the supervisor tick —
  and git in the checkout reads the checkout's own config, which the box can write
  (`core.fsmonitor`, filter drivers named by attributes: host code execution; GHSA-j5mq-v7p4-qw2j,
  whose 0.3.2 fix only hardened those calls). Now the host runs **no process** for it. The shim,
  after a box command moves HEAD, builds the healed index from a COPY of the shared one with the
  box's own git (same decisions as before: pure staleness → the new HEAD's tree; staged host work
  disjoint from the move → carried; overlapping → refused) and offers it as a new file,
  `index.fy-proposed.<head>.<base>.<ff|carry>`, where `<base>` is the copy's blob id. The
  supervisor (`githeal.py`) installs it under `index.lock` only if the shared index is still
  byte-identical to `<base>` and HEAD (read as data from loose refs / `packed-refs`; a reftable
  repo is not healed) is still `<head>` — re-read under the lock and again after the write, which
  restores the index if HEAD moved, since `index.lock` does not guard refs (a `reset --soft`
  moves HEAD without it) — so host staging since the proposal is never lost; the box
  simply re-proposes on its next index-touching call. It installs the bytes it read and checked
  (a no-follow read of a regular file, via `mountwrite`), never a rename of the box's file, and
  reaches the git dir from the trusted main checkout without following a symlink (a worktree's
  `.git` file contributes only a NAME under `<main>/.git/worktrees/`). The box could write
  `.git/index` directly anyway, so a proposal grants it nothing new.
  **The visibility this rests on, measured** (Lima `vz`, virtiofs, 2 MB payloads): a file the VM
  writes to a temp name and renames into place reaches the host whole — 0 torn, 0 short, never
  missing between versions, never older than one seen, all versions observed across 14.5k reads;
  a new file round-trips host→VM→host in 0.6 ms (p95 0.7). The control (the VM overwriting in
  place) tore 393 times, so the check detects tearing. **The reverse does NOT hold:** a file the
  HOST replaces reads as *missing* from the VM for 22 ms to ~1 s (the guest's cached entry) — and
  on podman machine's libkrun virtiofs for up to ~5 s: there a replaced name comes back only at
  the next 5-second cache boundary (measured 2026-09-28: new versions visible at t ≈ 5, 15, 20,
  30, 35 s; windows 0.2–4.7 s). So every box offer is a new name and nothing box-side re-reads a
  name the host rewrites — and the hazard is broader than the heal: host-side git replaces
  `HEAD`, refs and `packed-refs` the same way, and the shim's one-time seed of `index-box` from
  `.git/index` can land in that window. The seed therefore retries for ~6 s and falls back to
  HEAD's tree; it treats an unresolvable HEAD as unborn only when the reflog (appended in place,
  never replaced) is empty too, since a host commit hides the branch ref in the same window; and
  it publishes under git's own `index-box.lock`, only while `index-box` is still absent, so a
  racing first call that already seeded and staged is never overwritten.
  `tests/test_mount_visibility_e2e.py` re-checks the direction the heal needs on every host tier
  (9p on the Linux and WSL2 runners).
  **The box installs it itself, sealed to its ref move** (2026-09-29). Waiting for the host's tick
  left a gap: a host commit between the box's branch move and the heal recorded the box's files
  as deleted from the stale shared index (an IDE's `git add` polling on the host: 3 reverts in 46
  box commits on libkrun). The box's own transaction now builds the healed index at `prepared` —
  from a copy, with the same carry rule — takes `index.lock` (waiting ~1 s for a host git
  holding it; rebuilding under it if the index changed since the copy) and renames the build in
  at `committed`; `aborted` drops it, and a git killed in between leaves no lock (the shim's
  post-command step releases it). A host commit needs that same lock, so it can't land in the
  gap. The proposal/rename path above stays as the fallback (an operation in progress, a lock
  held too long). The carry asks git twice whatever the number of staged paths (two `diff-index`
  calls paired in step) — asked per path, it held git's ref lock ~28 s for 19 host-staged paths
  on a loaded VM and every host commit failed "HEAD.lock: File exists" meanwhile. It refuses
  rather than guesses: a sync point git can't read, a conflict entry (a host `stash pop` leaves
  them with no operation state) and an entry that doesn't pair up (a host file where the box made
  a directory) all refuse instead of carrying "nothing" — each of which dropped or reverted
  staged work before. The seal needs a sync point: a shared index that has never had one (a fresh
  worktree) gets it from the box's first git call, rather than from the host's tick up to ~2 s
  later — the agent's first commit in every fresh worktree went unsealed until then.
- Stray `index-box` files are derived state — safe to delete anytime (the shim re-seeds).
- Follow-up: an `fy verify` assertion that in-box `git` resolves to the shim, so a regression
  fails loudly instead of silently re-arming the race.

## Rejected alternatives

- **Dropping the VM's file-lookup cache** (2026-09-29; a root loop writing `drop_caches`) — every
  50 ms it shrinks the stale window to ~0.1 s and makes plain git safe in the measured flows, but
  `git status` gets 8–9× slower for every container in the VM, and working-tree files still read
  absent 7.5% of the time. With the shim covering git, it isn't worth that cost.
- **A time budget for the sealed heal** (2026-09-29) — giving the seal up when it runs long
  would spare host commits a "HEAD.lock: File exists", but it gives the seal up exactly when the
  VM is loaded, which is when a host commit lands in the gap and silently reverts the box's
  files. A refused host commit is retried; a revert isn't noticed. The carry was made cheap
  instead.
- **Judging every `HEAD` line as a move of the checked-out branch** (2026-09-29) — it would catch
  a box whose view and git's disagree about whether HEAD is detached, but git sends no expected
  value for `checkout --detach <older>`, so detaching to an older commit would be refused every
  time. The unborn-with-history check covers the case that went wrong.

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
- **A periodic worktree snapshot** (host-side, on the supervisor tick — modelled on
  `transcripts.sweep`: tick-counted interval, edge-triggered logging, never raises) — a time
  machine for the checkout backed by git's own object store: build a tree through a throwaway
  index, `commit-tree` it, point a ref at it, write no ref when the tree is unchanged. Verified
  2026-09-08 (git 2.47): the real index and worktree are untouched, `.gitignore` is honoured (so
  `node_modules` costs nothing), and it runs in 13 ms warm / 60 ms cold on a 194-file repo
  measured *over the mount* — the supervisor is host-side, so in practice it reads the checkout
  natively and never pays the virtiofs metadata tax a box-side equivalent would. Note `git stash
  create` is **not** the primitive to reach for: it silently omits untracked files, which is
  exactly the never-staged content that is unrecoverable (a staged blob survives as dangling and
  can be fished out with `fsck`; an unstaged one leaves no trace at all).
  It would blunt two residuals above — the stale-side `commit` that silently reverts the other
  side's work, and the shared-refs weakness — and, by keeping uncommitted work host-side, it
  retires the durability objection that gates the VM-native alternative.
  Not taken now, on three counts: it nets a *different* failure (deliberate destruction —
  `checkout --`, `reset --hard`, `clean -fdx`, `rm -rf`) than the corruption this ADR closes; it
  needs a retention policy, since refs are reachable and `gc` will therefore never prune them; and
  it needs a deliberate answer on where the store lives — inside `.git` is sufficient for
  accidents (none of those verbs touch refs) while only an outside-the-mount store under
  `state_dir()` holds against a box that is actively hostile, which is a security claim rather
  than a usability one. The narrow, event-triggered slice of this idea **was** taken: `worktree
  remove` parks a doomed checkout's uncommitted work on `refs/fy/removed/<name>-<ts>` in main
  (2026-09-08), where the destruction is certain and the moment is known.
