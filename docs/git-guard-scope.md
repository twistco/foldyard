# Keeping the box out of what your git runs: scope

Status: **proposed, nothing built**, 2026-11-18. Two parts: a guard that makes the parts of `.git/`
your git can be made to run read-only from the VM, and `fy share`, a way to give a box another
repository without your git ever running in the copy. Related:
[ADR-0023](./adrs/0023-no-host-executed-code-from-the-repo-mount.md) (this is its first remaining
channel), [ADR-0033](./adrs/0033-a-to-box-and-a-from-box-beside-the-checkout.md) (read-only that
holds outside the box), [ADR-0032](./adrs/0032-a-root-collector-in-the-guest.md) (an opt-in feature's
own root script in the guest), [ADR-0021](./adrs/0021-per-kernel-git-index-split.md) (the box's own
index), and [security.md](./security.md)'s "Git's own hooks run on your computer".

## The problem

The VM mounts the checkout read-write, `.git/` included, so a box can write everything your git
reads. security.md says so and stops there: "foldyard can't close this without owning your
`.git`, so treat a shared checkout's hooks as code you are choosing to run." foldyard's own git on
your computer runs none of it (`fy worktree add` and `remove` switch hooks, fsmonitor, filters and
transports off). Yours runs all of it, and so does a git app such as Fork, which also runs
`git status` in the background while a repository is open: a `core.fsmonitor` command the box wrote
runs without you doing anything.

What makes this hard to fix by asking people to change habits: a colleague told to keep a second,
host-only clone for pushing, or to stop using their git app on the checkout, keeps the old workflow
on their laptop with no box at all. A fix has to leave your git and your git app exactly as they
are, and take the write away from the box instead.

### What in a repository can make your git run something

From the git directory:

- **`config`**, and every file it includes: `core.hooksPath`, `core.fsmonitor`, `core.sshCommand`,
  `core.pager`, `core.editor`, `core.askPass`, `credential.helper`, `gpg.program`, filter, diff and
  merge drivers, `!` aliases, `[hook]` entries (git 2.54), `url.<x>.insteadOf` and
  `remote.<name>.uploadpack` (where a fetch or push connects, and with what).
- **`hooks/`**.
- **`modules/<name>/`**: each submodule's own `config` and `hooks/`.
- **`worktrees/<name>/commondir`**: where a linked worktree's git finds the shared directory, and
  so its `config`. A box that rewrites it points your git in that worktree at a `config` of its
  choosing.

From the working tree:

- **A worktree's `.git` file** (`gitdir: …`), which the same way can point your git at another git
  directory.
- **A `.git` the box creates in a subdirectory.** Git takes the first `.git` it finds walking up
  from where it runs, so a shell prompt that shows git status in `src/` would use a planted
  `src/.git`. Fork opened on the repository root doesn't look there.
- **Hooks that live in tracked files**: husky's `core.hooksPath = .husky`, lefthook's
  `lefthook.yml`, `.pre-commit-config.yaml`. The setting that runs them is in `config`; what they
  run is a tracked file the box can edit.

`.gitattributes` and `info/attributes` only name a filter; the command is in `config`.
`objects/info/alternates` makes your git read objects from another path, which runs nothing.

## Part 1: the guard

**Guest root, at boot, bind-mounts the dangerous parts of each `.git/` read-only over
themselves.** Rootless podman builds its mount namespace from the guest's, and the kernel locks
every mount it copies into a namespace owned by a less privileged user namespace: the VM user, the
box and any container started through the engine socket can't remount them read-write or unmount
them to see what is underneath. A non-recursive bind of the checkout (`--mount
type=bind,bind-nonrecursive`), which would otherwise show the writable files under a submount, is
refused while the directory holds locked mounts. That is the kernel's own rule for exactly this
case, and the reason the guard sits in the guest rather than in the box's create arguments: a box
can start a container with any mounts it likes, but only from inside podman's namespace.

What gets bound, per checkout the VM mounts:

| path | bound | why |
| -- | -- | -- |
| `.git/` | **onto itself, read-write** | a mount point can't be renamed: otherwise the box moves `.git` aside and makes a new one, with its own `config`, where yours used to be |
| `.git/hooks/` | read-only | |
| `.git/config` | read-only (see the open question below) | |
| `.git/modules/` | read-only | submodule `config` and hooks; a box that needs to init a submodule can't |
| each registered worktree's directory | onto itself, read-write | the same rename, one level up |
| each registered worktree's `.git` file, and its `commondir` | read-only | the pointers above |

Everything else in `.git/` stays writable, as the box's git needs it to be: objects, refs, `HEAD`,
the index and the box's own `index-box`, `logs/`, `packed-refs`, the lock files beside them, and
`worktrees/` itself, so an agent's quick worktree under `.claude/worktrees/` still works.

### What the box can no longer do, and how it copes

Writing `.git/config` is rarer in a box than it sounds, and every case has a way round:

- **`git branch -u` and `--set-upstream-to`** record which remote branch a local branch follows
  (`branch.<name>.remote` and `.merge` in `.git/config`), which `git status` uses for ahead/behind
  and a bare `git pull` for its source. The box can't push, so it rarely matters there.
- **Tracking set up as a side effect**: `git switch <name>` when only `origin/<name>` exists, or
  `git switch -c x origin/x`, writes the same keys. foldyard sets `branch.autoSetupMerge = false` in
  the box's own global config (the same place it puts your git identity and `[box] git_hooks`), so
  those commands make the branch without trying to. `git worktree add --no-track`, the documented
  quick worktree, already writes nothing there.
- **`git remote add`, `git config <key>` without `--global`, `git submodule init`, `git lfs
  install`** fail with "could not lock config file". `--global` writes to the box's own config,
  which is the right place for a box setting anyway. The bundled skill says so.

### What the guard doesn't cover

- **Hooks run from tracked files** (husky, lefthook, pre-commit). The guard protects the setting,
  not what it runs. A doctor row can at least say when `core.hooksPath` points into the working
  tree, or a hook manager's file is present, so the risk is named rather than discovered.
- **A `.git` planted in a subdirectory**, met by git run below the root (a shell prompt, an editor's
  git integration on a subfolder). Named in security.md; nothing in the guard reaches it.
- **Anything your computer runs from a repository the VM doesn't mount.** Not the guard's job.

### Running VMs and running boxes

Guest root work happens at boot, by foldyard's rule: `guest-boot.sh` is the one place, and changing
a byte of it makes every VM stale. So the guard is an opt-in feature with its **own** recorded boot
script under its own marker, as the guest monitor is, which always exits 0 and reports what it did
in `/run/fy-git-guard/`; the host fails closed when the report doesn't match what it asked for, as
it does for the wall.

The bind mounts exist only for what is there at boot. For what comes later (a worktree from
`fy worktree add`, a submodule cloned since) a root unit in the guest takes requests the way the
monitor's installer does: the host writes the path into a VM-user inbox, a root `.path` unit checks
it is under a mounted checkout, isn't a symlink, and binds it. That reaches podman's namespace and
every container created afterwards. **A box that is already running doesn't see it**: crun gives a
container private mounts (found for `crossmask`, 2026-10-27), and `crossinject`'s way in mounts as
the VM user, which the box can take off again. So a new worktree is guarded from its box's
creation, which is when `fy worktree add` makes it anyway, and turning the guard on means recreating
boxes, which `fy up` already says after a `[box]` change.

### The open question: `.git/config` is a file

A bind mount of a file holds the file the mount was made on. Git doesn't write `config` in place: it
writes `config.lock` and renames it over `config`. In the box that rename fails (the target is a
mount point), which is the point. **On your computer it succeeds**, and Fork does it whenever it
sets an upstream, on every first push of a new branch. What the guest then sees is the spike's
first question. Two outcomes are plausible:

- the bind keeps the old file: the box reads stale config (a new remote or upstream doesn't show)
  until the guard re-binds, and boxes created before that keep the old one;
- the kernel notices the name now points elsewhere and detaches the mounts on it (it does this for
  network filesystems whose entries go stale): the guard silently disappears, everywhere, until
  re-bound — and in a running box, until it's recreated.

The directories (`hooks/`, `modules/`) don't have this problem: nothing on your computer replaces
them. If `config` turns out not to be bindable reliably, the fallback is to guard `hooks/`,
`modules/` and the pointers, and **report** `config` instead: your computer keeps a fingerprint of
the keys above and the supervisor notifies you on a change, as it does for an unadopted
`foldyard.toml`. That narrows the gap to "you are told before your git next runs", which is weaker
than read-only, and honest about it.

### Verification

`fy verify` gets a row that proves the guard the way the to-box row does, by trying: from a
`--privileged` container started through the engine, with the checkout mounted, write to
`.git/config` and `.git/hooks/`, remount them read-write, unmount them, rename `.git`, and mount the
checkout non-recursively elsewhere and write through that. Every one refused is a PASS; any that
lands is a FAIL naming it. `fy doctor` gets a `git guard` row: on, and how many paths are bound
versus asked for.

## Part 2: `fy share`, another repository in the box

A copy inside the checkout, made on your computer, that the box can read and edit, and whose work
comes back as commits without your git ever running in the copy. It needs no new mount and works
on every backend, since the checkout is already shared. It is the pattern claude-code-log's
`just clone-real-transcripts` uses for transcripts, made safe to run from a box-writable checkout.

| verb | does |
| -- | -- |
| `fy share add <path> [--name n]` | copies the repository, everything including untracked files and `.git`, to `.devbox-share/<name>/`; copy-on-write where the filesystem has it (APFS, btrfs, XFS), a full copy where it doesn't |
| `fy share pull <name>` | the box's git writes a bundle of the copy's branches, streamed out over `podman exec`; your computer saves it under `~/.foldyard/<project>/shares/` and fetches it into the **original** as `refs/remotes/box-<name>/*` |
| `fy share refresh <name>` | copies the original in again, refusing while the copy has commits `pull` hasn't brought back |
| `fy share ls` / `rm <name>` | what's shared, and removal |

**Why a bundle.** It is a remote packed into one file: commits and branch names, nothing else.
Fetching from it runs nothing from the copy (no config, no hooks, no filters), so the copy's `.git`
needs no guard, and doesn't get one. `fy share add` can also add the saved bundle to the original
as a remote named `box-<name>`, so getting work back in Fork is `fy share pull`, then Fetch. Git
only ever reads the copy that `fy` saved outside the checkout: a bundle path the box can write
could be swapped for a crafted repository directory, and cloning from one was a git CVE in 2024.

**Your computer never runs git in the copy.** That is the rule the design rests on, and the one
thing `fy share` can't enforce: opening the copy in Fork, or `cd`-ing into it with a git prompt,
runs whatever the box wrote there. `fy share add` says so, and the copy lives under a dotted folder
nobody opens by accident.

**Writing into the checkout safely.** The copy is made in a staging folder under `~/.foldyard/` on
the same volume and renamed into place through directory handles that refuse symlinks
(`mountwrite`), so a link the box planted at `.devbox-share` can't send it anywhere else. Where the
volumes differ, `cp -c` can't clone across them and it is a full copy. Untracked files mean
secrets such as a `.env` reach the box too; that is the point of "everything", and `add` lists what
untracked files it carried.

**Per checkout.** A share lives in the checkout it was made in; a worktree makes its own.
`.devbox-share/` goes into `fy init`'s managed ignore block.

## Spike

A host-tier module, `tests/test_git_guard_e2e.py`, on a VM with a test-only boot script (own
marker, always exits 0) doing the binds, as `test_mounts_e2e.py` does for the to-box. Each
question with a red-first control:

1. **The lock holds.** From the box and from a `--privileged` container started through the
   engine: write, `chmod`, remount read-write, unmount, rename `.git`, and a non-recursive bind of
   the checkout then a write through it. All refused. Control: without the boot script, every one
   lands.
2. **`.git/config` replaced on your computer.** `git config` on the host (a rename over the file),
   then what the guest's root namespace, a new container and a running box each see, and whether
   the mount is still there. On vz with virtiofs and on qemu with 9p, since the two filesystems
   decide differently when a name is stale.
3. **Guest root into a running box.** Whether a mount guest root moves into a running box's
   namespace can be taken off by the box. Expected: yes, it can, which keeps "recreate the box";
   worth one test before designing round it.
4. **What agents trip on.** A scripted round in a guarded box with `branch.autoSetupMerge = false`:
   commit, rebase, stash, a quick worktree with `--no-track`, switching to a branch only `origin`
   has, `gc`. Each either works or fails with the "could not lock config file" message the skill
   explains.
5. **Fork.** Off the suite, by hand: push a new branch from Fork on a guarded checkout, then run
   question 2's checks.

## Phases

0. The spike, and a decision on `.git/config` from question 2.
1. The guard, experimental, `[machine] git_guard = true`: the boot-time binds for the main checkout
   and registered worktrees, the verify and doctor rows, `branch.autoSetupMerge` in the box's
   config, the skill's paragraph. Turning it on recreates boxes.
2. The root unit for paths added while the VM runs (`fy worktree add`).
3. `fy share`, independent of the guard and buildable alongside it.
4. The guard on by default, once phase 1 has been used day to day.

## Decisions for the maintainer

1. `.git/config`: bind it (if question 2 allows), or guard the rest and report it.
2. The guard's switch: under `[machine]` (it is guest provisioning) or `[box]` (it is about the
   box's reach).
3. `fy share`'s folder: `.devbox-share/` (one more entry in the managed block, so existing projects
   need it added) or under `.devbox-foldyard/`, which every project already ignores.
4. Whether `fy share add` adds the bundle remote to the original by itself, or prints the
   `git remote add` line for you to run.
