# Keeping the box out of what your git runs: scope

Status: **Part 1's read-only guard was spiked on 2026-11-18 and does not hold** on a macOS host (vz,
virtiofs); what's left of Part 1 is reporting. Part 2, `fy share`, is unchanged by the spike.
Nothing is built. Related: [ADR-0023](./adrs/0023-no-host-executed-code-from-the-repo-mount.md)
(this is its first remaining channel),
[ADR-0033](./adrs/0033-a-to-box-and-a-from-box-beside-the-checkout.md) (read-only that holds outside
the box), [ADR-0032](./adrs/0032-a-root-collector-in-the-guest.md) (an opt-in feature's own root
script in the guest), and [security.md](./security.md)'s "Git's own hooks run on your computer". The
kit that ran the spike: [git-guard-spike-kit/](./git-guard-spike-kit/).

## The problem

The VM mounts the checkout read-write, `.git/` included, so a box can write everything your git
reads. security.md says so and stops there: "foldyard can't close this without owning your `.git`,
so treat a shared checkout's hooks as code you are choosing to run." foldyard's own git on your
computer runs none of it (`fy worktree add` and `remove` switch hooks, fsmonitor, filters and
transports off). Yours runs all of it, and so does a git app such as Fork, which also runs
`git status` in the background while a repository is open: a `core.fsmonitor` command the box wrote
runs without you doing anything.

What makes this hard to fix by asking people to change habits: a colleague told to keep a second,
host-only clone for pushing, or to stop using their git app on the checkout, keeps the old workflow
on their laptop with no box at all. A fix has to leave your git and your git app exactly as they
are.

### What in a repository can make your git run something

From the git directory:

- **`config`**, and every file it includes: `core.hooksPath`, `core.fsmonitor`, `core.sshCommand`,
  `core.pager`, `core.editor`, `core.askPass`, `credential.helper`, `gpg.program`, filter, diff and
  merge drivers, `!` aliases, `[hook]` entries (git 2.54), `url.<x>.insteadOf` and
  `remote.<name>.uploadpack` (where a fetch or push connects, and with what).
- **`hooks/`**.
- **`modules/<name>/`**: each submodule's own `config` and `hooks/`.
- **`worktrees/<name>/commondir`**: where a linked worktree's git finds the shared directory, and so
  its `config`.

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

**`config` is the one that matters.** Whoever can write it chooses `core.hooksPath`, so protecting
`hooks/` or any pointer without it protects nothing (the spike showed your git following a
`core.hooksPath` a box had set).

## Part 1: a read-only guard, tried and set aside

The idea: guest root, at boot, bind-mounts those parts of each `.git/` read-only over themselves,
and pins `.git` and each worktree's directory (a mount point can't be renamed, so the box can't move
`.git` aside and put its own in its place). Rootless podman builds its mount namespace from the
guest's, the kernel locks mounts copied into a namespace owned by a less privileged user namespace,
and so neither the box nor anything it starts through the engine socket could take them off.

### What the spike found

On a throwaway clone of the base VM (Fedora 44, kernel 6.19.10, podman 5.8.9, vz with virtiofs on
macOS), from a container shaped like the box (`--user 0`, the checkout bound at its own path) and a
`--privileged` sibling, each of which a box can start through the socket. Three runs: one by hand,
then two of the kit, which agreed with each other line for line.

| when | result |
| -- | -- |
| no guard (the control) | every write lands from both; the sibling can also bind the share again non-recursively and write through that |
| guard added while podman's namespace already existed | **not locked**: the sibling unmounts and remounts the binds. Run 1: writes land in new containers. Kit runs: new containers see `.git` empty, so git in them is broken |
| guard in place before podman's namespace (what a boot-time guard would get) | **the lock works**: read-only holds against the box and the sibling, neither can remount or unmount, a non-recursive bind fails at create (crun refuses it), and `/proc/1/root` is refused. In run 1, with every bind intact, the pins, `hooks/`, `commondir` and the worktree's `.git` all held |
| a box started before the guard, renaming through the share | everything lands for it, as it sees no guard; afterwards the guard is gone **from the guest itself**: its `git config` (a rename over `config`) took that bind away, and in the kit runs renaming `.git` aside and back took every bind with it, pins included |
| your computer renames over `config` (`git config`; Fork's upstream on a first push does the same) | `findmnt` still lists the bind. Run 1: it no longer protects anything (the VM user's write lands, a box sets `core.hooksPath`, and your git reads it). Kit runs: containers can't read `.git/config` at all, so git in the box fails ("unknown error occurred while reading the configuration files") |
| your computer appends to `config` in place | the bind survives |

The mechanism, as far as a spike can tell: a bind mount in the guest is attached to the guest's
cached entry for that name. A rename through the share (from your computer, or from any namespace
where the name isn't a mount point) replaces what the name means on the host side, and when the
guest next checks the entry it finds it stale. It then either drops the mounts on it or keeps them
on an entry the path no longer leads to. Which one, and when, depends on lookup timing, which is why
the hand run and the kit differ.

### Why that rules it out

Git writes `config` only by renaming a new file over it, and your computer's git does so routinely
(Fork sets an upstream on every first push of a branch). Each time, the guard on the one file that
matters either stops protecting without saying so or breaks git in every box. A re-bind afterwards
doesn't help: the second row shows a mount added while podman's namespace exists isn't locked, so a
box could take it off. And the directories don't survive a rename of anything above them either.

What the spike did establish, and is worth keeping: **mounts guest root makes before podman's
namespace exists are locked against a box and everything it can start**, privileged siblings,
non-recursive binds and `/proc/1/root` included. That holds for anything on the guest's own
filesystems; it is the shared folder, renamed under the guest from outside, that defeats it here.

Not tested: 9p on Linux and WSL2 hosts (a different cache, but the same guest-side entries renamed
from outside), whether a mount moved into a running box's namespace can be taken off (moot), and
what agents trip on with a read-only `config` (moot).

### What's left: telling you before your git runs it

A guard can't stop the write, so make the write visible. The supervisor already runs a tick per
project on your computer; each tick it fingerprints what can make your git run something, in each
registered checkout:

- in `.git/config` and every file it includes, the keys listed above (not the rest: Fork's
  `branch.<name>.remote` and `.merge` writes, for instance, aren't watched);
- `hooks/`, and `modules/*/config` and `modules/*/hooks/`;
- each registered worktree's `.git` file and `commondir`;
- whether `core.hooksPath` points into the working tree, or a hook manager's file is present (husky,
  lefthook, pre-commit), named once rather than watched, since its content is the project's.

The first fingerprint is taken when you turn it on, as your accepted state. A change notifies you
once (a desktop notification where there is one, as the supervisor's blocked-daemon alert does), a
doctor row shows it until you look, and a verb shows the difference and accepts it, the way
`fy config diff` and `fy config adopt` work for `foldyard.toml`. Reading `config` this way never
runs git there: a file read, with includes followed by foldyard's own reader, as `fy worktree add`
already does to switch `[hook]` entries off.

It is weaker than read-only, and honest about it: between the box's write and your next git command
there is a race, which a tick of a few seconds narrows and doesn't close. An app like Fork,
refreshing in the background, can lose that race.

### The structural option left, unproven

A **directory** mount survives where a file mount doesn't, because nothing renames `.git` itself
(the pin makes that so for the box, and your git never does). A FUSE filesystem in the guest,
mounted over `.git` by guest root before podman starts, could pass every operation through to the
real directory except writes to the paths above. Costs: every git operation in the box goes through
a second FUSE layer on top of virtiofs (`git status` on a large repository reads `.git` heavily), a
root daemon in the guest the box must not be able to stop, and a new spike. Worth one only if
reporting proves not to be enough.

## Part 2: `fy share`, another repository in the box

A copy inside the checkout, made on your computer, that the box can read and edit, and whose work
comes back as commits without your git ever running in the copy. It needs no new mount and works on
every backend, since the checkout is already shared. It is the pattern claude-code-log's
`just clone-real-transcripts` uses for transcripts, made safe to run from a box-writable checkout.

| verb | does |
| -- | -- |
| `fy share add <path> [--name n]` | copies the repository, everything including untracked files and `.git`, to `.devbox-share/<name>/`; copy-on-write where the filesystem has it (APFS, btrfs, XFS), a full copy where it doesn't |
| `fy share pull <name>` | the box's git writes a bundle of the copy's branches, streamed out over `podman exec`; your computer saves it under `~/.foldyard/<project>/shares/` and fetches it into the **original** as `refs/remotes/box-<name>/*` |
| `fy share refresh <name>` | copies the original in again, refusing while the copy has commits `pull` hasn't brought back |
| `fy share ls` / `rm <name>` | what's shared, and removal |

**Why a bundle.** It is a remote packed into one file: commits and branch names, nothing else.
Fetching from it runs nothing from the copy (no config, no hooks, no filters), so the copy's `.git`
needs nothing guarded. `fy share add` can also add the saved bundle to the original as a remote
named `box-<name>`, so getting work back in Fork is `fy share pull`, then Fetch. Git only ever reads
the copy `fy` saved outside the checkout: a bundle path the box can write could be swapped for a
crafted repository directory, and cloning from one was a git CVE in 2024.

**Your computer never runs git in the copy.** That is the rule the design rests on, and the one
thing `fy share` can't enforce: opening the copy in Fork, or `cd`-ing into it with a git prompt,
runs whatever the box wrote there. `fy share add` says so, and the copy lives under a dotted folder
nobody opens by accident. It is the same exposure as the checkout itself, no more; Part 1's report
could cover shares too.

**Writing into the checkout safely.** The copy is made in a staging folder under `~/.foldyard/` on
the same volume and renamed into place through directory handles that refuse symlinks
(`mountwrite`), so a link the box planted at `.devbox-share` can't send it anywhere else. Where the
volumes differ, `cp -c` can't clone across them and it is a full copy. Untracked files mean secrets
such as a `.env` reach the box too; that is the point of "everything", and `add` lists what
untracked files it carried.

**Per checkout.** A share lives in the checkout it was made in; a worktree makes its own.
`.devbox-share/` goes into `fy init`'s managed ignore block.

## Running the spike again

`docs/git-guard-spike-kit/run.sh [vm-name]` on a host with Lima and a baked base
(`fy machine base bake`): it clones the base into a throwaway VM, which it deletes at the end, and
touches no project VM. `guard.sh` is the guard (guest root), `attack.sh` every attempt, one line
each, `LANDED` or `refused`, undoing what lands. The rounds are the table's rows in order. On a
Linux or WSL2 host the clone would use 9p, the row this scope hasn't seen.

## Phases

1. The report (Part 1, what's left), experimental: the fingerprint and its tick, the notification,
   the doctor row, the verb to see and accept a change.
2. `fy share`, independent of the report and buildable alongside it.
3. The FUSE spike, only if the report's race proves too wide in use.

## Decisions for the maintainer

1. The report's verb: under `fy config` (it is the same adopt-what-changed shape) or a `fy git`
   group of its own.
2. Whether a pending change also gates the launch verbs (`fy up`, `fy box up`), as an unadopted
   `foldyard.toml` does, or only notifies.
3. `fy share`'s folder: `.devbox-share/` (one more entry in the managed block, so existing projects
   need it added) or under `.devbox-foldyard/`, which every project already ignores.
4. Whether `fy share add` adds the bundle remote to the original by itself, or prints the
   `git remote add` line for you to run.
