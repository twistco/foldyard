# Git in the box — one checkout, two sides

Your repo lives on your computer, and the VM mounts it, so the box and your computer work on the
**same files and the same `.git`**. A commit made on one side is on the other at once. That's the
point, but it means two operating systems are writing one git repository over a shared mount, and
git was never designed for that. This page covers what foldyard does about it, what that changes
for you, and the few things not to do. Terms in bold are in the [glossary](./glossary.md).

## The box's git is a wrapper

Git keeps what you've staged in one file, `.git/index`, and it rewrites that file far more often
than you'd think: a plain `git status` refreshes it, so an editor or git app that just *shows* the
repo on your computer is writing it too. Git's way of replacing the file safely only holds within
one machine. Over the mount, two writers racing each other could leave it empty, and `git status`
then showed every file in the repo staged as deleted.

So `git` in the box is a small wrapper, the **git index shim**, that foldyard installs when it
creates the box. It works the same in any box image. It gives box-side git **its own index file**
(`.git/index-box`; each worktree gets its own too). Commits, branches and objects are still
shared, so nothing about sharing work changes. Only *staging* is now kept per side, and the two
sides never write the same index file.

That split has one catch, and the shim handles it. When one side commits or switches branch, the
other side's staging still describes the old commit. Committing it as it stands would quietly
undo the other side's work. So:

- Before each command in the box, the shim brings the box's index up to date with a move your
  computer made. Anything you'd staged in the box is carried along when it doesn't touch the
  files that changed.
- After a box commit, the box brings your computer's index up to date as well, under git's own
  lock, so your next `git status` there is right.
- When it can't do that safely (staged work on both sides touching the same files), it says so
  and refuses `git commit` until you sort it out, as below.

Over the mount, the box can also briefly see an *older* version of a branch your computer just
moved: measured at up to about a second on macOS with Lima, and up to five seconds on the podman
backend. The shim
checks every branch move under git's own lock, so a box command can't move a branch back over a
commit you just made.

The design and every case it covers are in [ADR-0021](./adrs/0021-per-kernel-git-index-split.md)
(`fy docs adr-0021`). To turn the shim off, set `[box] git_index_split = false`
([configuration](./configuration.md)); that brings the race back.

## When `git` refuses: "foldyard git shim: …"

Every refusal starts with `foldyard git shim:`, and refuses before anything is changed (most say
so: **nothing was changed**). Read the middle:

- **"your computer moved it while this ran"**, **"HEAD kept moving"**, **"reads as having no
  commits, but it has history"**, **"another git holds its lock"** — the box caught a stale view.
  Run the same command again.
- **"your computer is in the middle of a git operation (rebase-merge)"** (or a merge,
  cherry-pick, revert) — wait until it's finished on your computer, then run it again. Don't
  abort that rebase from the box.
- **"your computer has a leftover autostash"** — a `git pull --rebase` on your computer stopped
  before it began and left the changes it had set aside in its rebase directory. It won't clear by
  itself and `git rebase --abort` can't clear it. On your computer, `fy doctor` shows a
  `leftover autostash` row with the exact commands: keep the changes as a stash, remove the
  directory, then apply the stash.
- **"not moving … back from … to …"** — a command asked to move the branch to an older commit
  without saying what it expected to replace. If you meant that, use `git reset --soft
  <commit>` or `git reset <commit>` instead; if you didn't, your computer just moved the branch,
  so run it again.
- **"index-box is stale … can't be carried forward"**, then **"refusing 'git commit' on the stale
  index"** — recover in the order below.

Don't route around the wrapper (calling another `git` binary, `FY_GIT_SHIM_OFF=1`): those refusals
are what keeps a commit in the box from silently reverting one on your computer.

## When `git status` looks mass-wrong

Changes staged that nobody made, or whole folders staged as deleted, mean an index is stale, not
that your work is gone. **Never `git reset --hard`**: it overwrites your files with the last
commit and throws away uncommitted work.

A plain `git reset` (mixed) is *almost* right, but check `git status --short` first. A line
starting `AD` is a file that was staged and then removed from disk: its content now exists only
in the index, and a reset drops it. So write those files out first, then reset:

```bash
git checkout-index -a   # write out files that exist only in the index (leaves your files alone)
git reset               # unstage everything — mixed, never --hard
git add …               # stage again what you meant to commit
```

This is what the shim prints when it refuses. The same order works on your computer. There,
nothing guards git, so if the box has committed since you last staged, glance at `git status`
before you commit.

## The stash is shared

There is one `git stash list` for your computer, the box, every worktree and every agent. A bare
`git stash pop` takes whatever was stashed last, which may be someone else's. Label yours and
pop it by number:

```bash
git stash push -m my-label
git stash list            # find stash@{n} for my-label
git stash pop 'stash@{n}'
```

## Don't `git stash -u` or `git clean -d` while the stack is up

When a compose service bind-mounts a directory *inside* another bind mount (say `./app` is
mounted at `/srv`, and `./lib` at `/srv/lib`), the engine creates the inner mount point as an
empty directory in your checkout if it doesn't exist. Git ignores empty directories, so it shows
as nothing at all — but `git stash -u` and `git clean -d` delete untracked directories, and that
one goes with them. The containers keep running ("Up") while the service inside can no longer
find its files.

To compare against a clean tree, use a worktree, or a plain `git stash` (no `-u`). If it already
happened, recreate the directory (`mkdir -p <path>`) and restart the containers that mount it.

## The executable bit

The two sides of the mount don't always agree on a file's permission bits, so a script can look
modified just because its executable bit flipped. foldyard sets `core.fileMode = false` in the
main checkout's `.git/config` the first time it sets up the stack for that checkout (`fy up`,
`fy ps` or any stack command, and `foldyard shellenv`), and only writes it when it isn't already
set. Worktrees share that setting.

With it, git ignores executable-bit changes on disk, so `chmod +x` alone won't show up in a
commit. To commit a file as executable, tell git directly:

```bash
git update-index --chmod=+x path/to/script.sh
```

## Pushing

`git push` from the box is refused, by design: no SSH key, token or credential helper reaches it.
`fy verify` checks this with a dry-run push to `origin`. Commit in the box; push from your
computer, where your credentials live.

If the box needs to work on GitHub itself (open a pull request, reply to review comments), give it
a GitHub App switch — see [github-app.md](./github-app.md). That token is used for GitHub's API,
not for `git push`, and it can do exactly what the App's installation allows: an App with
`contents: write` can change the repo through the API, so `fy config widenings` shows what each
App was last read to grant.

Git hooks and aliases are a separate matter: the box can write `.git/hooks`, `lefthook.yml` or
`.git/config`, and they run on your computer the next time you use git there
([security](./security.md)).

## Worktrees

For parallel work, make a worktree on your computer with `fy worktree add <name>`: its own
checkout and branch (`wt/<name>` by default, forked from the trunk), its own box, stack, mode and
ports, all in the one VM. Don't run `git worktree add` in the box, and `fy worktree add` refuses
there: it would record paths in the shared `.git` that exist on only one side.

Your computer runs each checkout's *adopted* `foldyard.toml`, never the working tree
([configuration](./configuration.md#the-adopted-config-what-the-host-actually-runs)), so `fy worktree add`
settles the new checkout's adoption before it finishes. A worktree whose config is byte-identical
to the one adopted for main is adopted without a prompt, since it grants nothing main hasn't
already been granted: it says `✓ config adopted — identical to main's`. One that differs is shown
as a diff against main and adopted only if you answer the prompt; with no terminal, or if you
decline, the worktree is still made, nothing is adopted, and `WORKTREE=<name> fy config diff`
then `WORKTREE=<name> fy config adopt` (or the prompt at `WORKTREE=<name> fy box up`) finishes it.
`-y` only answers the question about registering an existing worktree; it never adopts a config.
When a worktree's config is adopted for the first time, main's keyless `claude`/`codex` modes are
copied into the worktree, so its box can use them from the start. That happens wherever the first
adoption is: at `fy worktree add`, at the prompt from `WORKTREE=<name> fy box up` or `fy up`, from
`WORKTREE=<name> fy config adopt`, or in the TUI. Only modes main has on are copied, never one
the worktree's mode already sets, and a later adoption copies nothing.

`fy worktree add <name> --box` then brings the worktree's box up, the same as running
`WORKTREE=<name> fy box up`. If its config wasn't adopted it doesn't start the box, and says so.

Every box sees every checkout: main's box reaches the worktrees, a worktree's box reaches main
and the other worktrees. Edit and read across them freely, but run installs in a checkout from
its own box. Each box keeps only its own checkout's `[box].shadow_volumes` dirs in volumes; the
same dirs of every other checkout are read-only in it, so `pnpm install` or `uv sync` there fails
instead of writing onto your computer's disk. The note in each of those dirs,
`RUN-IT-IN-ITS-OWN-BOX`, gives the command: from that directory, `WORKTREE=<name> fy box exec
'<command>'` runs it in the worktree's own box (`WORKTREE= fy box exec '<command>'` in main's), as
long as that box is running — `WORKTREE=<name> fy box up` on your computer starts it. A worktree
added after a box was created isn't masked in that box until it's recreated: `fy worktree add`
names the boxes it isn't masked in, and `fy doctor`'s `box shadowing` row reports it. Only
worktrees registered with your computer are masked: one made with plain `git worktree add` is
masked in no box, recreated or not, until `fy worktree add <name>` on your computer registers it
(it asks first). The row lists those apart, with that fix.

`fy worktree remove <name>` takes down that worktree's box, stack and volumes, deletes the
checkout and its adopted config, and keeps its branch, so a worktree added later under the same
name starts with nothing adopted. A worktree with uncommitted work needs `--force`; that work is
saved first on a ref in the main checkout (`refs/fy/removed/<name>-<time>`), and the command
prints how to restore it.

## What `fy doctor` checks

- **`git shim`** (in the box) — the `git` on PATH is foldyard's shim. If it isn't, the image put
  another git first on PATH or the install failed, and none of the above protects you. A new box
  runs this check once at the end of `fy box up`.
- **`git shim hooks`** (in the box) — the hooks the shim uses to check branch moves are installed,
  and only root can change them. The fix for either row: `fy box down && fy box up` on your
  computer.
- **`shared git config`** (both sides) — `.git/config` still has git's own keys and a remote. A
  config file emptied by a write race fails here, with the keys to restore; no remote is a
  warning, since `fy verify` then can't prove the push refusal.
- **`leftover autostash (<checkout>)`** (your computer) — the stuck `git pull --rebase` above,
  with its recovery commands.
