# Git in the box — one checkout, two sides

Your repo lives on your computer, and the VM mounts it, so the box and your computer work on the
**same files and the same `.git`**. A commit made on one side is on the other at once. That's the
point, but it means two operating systems are writing one git repository over a shared mount, and
git was never designed for that. This page covers what foldyard does about it, what that changes for
you, and the few things not to do. Terms in bold are in the [glossary](./glossary.md).

## The box's git is a wrapper

Git keeps what you've staged in one file, `.git/index`, and it rewrites that file far more often
than you'd think: a plain `git status` refreshes it, so an editor or git app that just *shows* the
repo on your computer is writing it too. Git's way of replacing the file safely only holds within
one machine. Over the mount, two writers racing each other could leave it empty, and `git status`
then showed every file in the repo staged as deleted.

So `git` in the box is a small wrapper, the **git index shim**, that foldyard installs when it
creates the box. It works the same in any box image. It gives box-side git **its own index file**
(`.git/index-box`; each worktree gets its own too). Commits, branches and objects are still shared,
so nothing about sharing work changes. Only *staging* is now kept per side, and the two sides never
write the same index file.

That split has one catch, and the shim handles it. When one side commits or switches branch, the
other side's staging still describes the old commit. Committing it as it stands would quietly undo
the other side's work. So:

- Before each command in the box, the shim brings the box's index up to date with a move your
  computer made. Anything you'd staged in the box is carried along when it doesn't touch the files
  that changed.
- After a box commit, the box brings your computer's index up to date as well, under git's own lock,
  so your next `git status` there is right.
- When it can't do that safely (staged work on both sides touching the same files), it says so and
  refuses `git commit` until you sort it out, as below.

Over the mount, the box can also briefly see an *older* version of a branch your computer just
moved: measured at up to about a second on macOS with Lima, and up to five seconds on the podman
backend. The shim checks every branch move under git's own lock, so a box command can't move a
branch back over a commit you just made.

The design and every case it covers are in [ADR-0021](./adrs/0021-per-kernel-git-index-split.md)
(`fy docs adr-0021`). To turn the shim off, set `[box] git_index_split = false`
([configuration](./configuration.md)); that brings the race back.

## When `git` refuses: "foldyard git shim: …"

Every refusal starts with `foldyard git shim:`, and refuses before anything is changed (most say so:
**nothing was changed**). Read the middle:

- **"your computer moved it while this ran"**, **"HEAD kept moving"**, **"reads as having no
  commits, but it has history"**, **"another git holds its lock"** — the box caught a stale view.
  Run the same command again.
- **"your computer is in the middle of a git operation (rebase-merge)"** (or a merge, cherry-pick,
  revert) — wait until it's finished on your computer, then run it again. Don't abort that rebase
  from the box.
- **"your computer has a leftover autostash"** — a `git pull --rebase` on your computer stopped
  before it began and left the changes it had set aside in its rebase directory. It won't clear by
  itself and `git rebase --abort` can't clear it. On your computer, `fy doctor` shows a
  `leftover autostash` row with the exact commands: keep the changes as a stash, remove the
  directory, then apply the stash.
- **"not moving … back from … to …"** — a command asked to move the branch to an older commit
  without saying what it expected to replace. If you meant that, use `git reset --soft <commit>` or
  `git reset <commit>` instead; if you didn't, your computer just moved the branch, so run it again.
- **"index-box is stale … can't be carried forward"**, then **"refusing 'git commit' on the stale
  index"** — recover in the order below.

Don't route around the wrapper (calling another `git` binary, `FY_GIT_SHIM_OFF=1`): those refusals
are what keeps a commit in the box from silently reverting one on your computer.

## When `git status` looks mass-wrong

Changes staged that nobody made, or whole folders staged as deleted, mean an index is stale, not
that your work is gone. **Never `git reset --hard`**: it overwrites your files with the last commit
and throws away uncommitted work.

A plain `git reset` (mixed) is *almost* right, but check `git status --short` first. A line starting
`AD` is a file that was staged and then removed from disk: its content now exists only in the index,
and a reset drops it. So write those files out first, then reset:

```bash
git checkout-index -a   # write out files that exist only in the index (leaves your files alone)
git reset               # unstage everything — mixed, never --hard
git add …               # stage again what you meant to commit
```

This is what the shim prints when it refuses. The same order works on your computer. There, nothing
guards git, so if the box has committed since you last staged, glance at `git status` before you
commit.

## The stash is shared

There is one `git stash list` for your computer, the box, every worktree and every agent. A bare
`git stash pop` takes whatever was stashed last, which may be someone else's. Label yours and pop it
by number:

```bash
git stash push -m my-label
git stash list            # find stash@{n} for my-label
git stash pop 'stash@{n}'
```

## Don't `git stash -u` or `git clean -d` while the stack is up

When a compose service bind-mounts a directory *inside* another bind mount (say `./app` is mounted
at `/srv`, and `./lib` at `/srv/lib`), the engine creates the inner mount point as an empty
directory in your checkout if it doesn't exist. Git ignores empty directories, so it shows as
nothing at all — but `git stash -u` and `git clean -d` delete untracked directories, and that one
goes with them. The containers keep running ("Up") while the service inside can no longer find its
files.

To compare against a clean tree, use a worktree, or a plain `git stash` (no `-u`). If it already
happened, recreate the directory (`mkdir -p <path>`) and restart the containers that mount it.

## The executable bit

The two sides of the mount don't always agree on a file's permission bits, so a script can look
modified just because its executable bit flipped. foldyard sets `core.fileMode = false` in the main
checkout's `.git/config` the first time it sets up the stack for that checkout (`fy up`, `fy ps` or
any stack command, and `foldyard shellenv`), and only writes it when it isn't already set. Worktrees
share that setting.

With it, git ignores executable-bit changes on disk, so `chmod +x` alone won't show up in a commit.
To commit a file as executable, tell git directly:

```bash
git update-index --chmod=+x path/to/script.sh
```

## Pushing

`git push` from the box is refused, by design: no SSH key, token or credential helper reaches it.
`fy verify` checks this with a dry-run push to `origin`. Commit in the box; push from your computer,
where your credentials live.

If the box needs to work on GitHub itself (open a pull request, reply to review comments), give it a
GitHub App switch — see [github-app.md](./github-app.md). That token is used for GitHub's API, not
for `git push`, and it can do exactly what the App's installation allows: an App with
`contents: write` can change the repo through the API, so `fy config widenings` shows what each App
was last read to grant.

Git hooks and aliases are a separate matter: the box can write `.git/hooks`, `lefthook.yml` or
`.git/config`, and they run on your computer the next time you use git there
([security](./security.md)). foldyard's own git on your computer runs none of them:
`fy worktree add` records the new worktree with the repo's hooks off (the hooks directory and
`[hook]` entries in `.git/config` alike) and says which it skipped. So a `post-checkout` hook that
sets a checkout up (lefthook's, say) hasn't run in a new worktree; git in that worktree's box runs
it at the next checkout there, and setup every new worktree needs belongs in
`[project].worktree_init`, which runs in a container ([configuration](./configuration.md)).

### Hooks that run only in the box

Hooks in the checkout (a `.githooks` directory, say) can run in the box without running on your
computer: set `[box] git_hooks = ".githooks"` ([configuration](./configuration.md)). foldyard sets
`core.hooksPath` in the box's own git config, for this checkout and its worktrees only, and never in
the shared `.git/config`. So the box's commits run your linters and formatters, and git on your
computer runs code the box can edit only if you turn it on there yourself
(`git config core.hooksPath .githooks` in the checkout). If you do, the box's setting is the same
and doesn't conflict; a different value in `.git/config` wins, and `fy box up` says so.

Lefthook installs into `.git/hooks` by default, which git on your computer runs as well. It doesn't
need this setting. In the box, foldyard runs every repo hook with `LEFTHOOK_NO_AUTO_INSTALL=1`, so a
lefthook release that has the variable (evilmartians/lefthook#1592, merged after 2.2.2 and not yet
released) doesn't re-sync its hooks from inside one.

Filter drivers can't be switched off like that, and a checkout runs them: `.git/config` can name a
command for each file `.gitattributes` routes through it (Git LFS's smudge filter is one; a box
could add its own). So `fy worktree add` doesn't check files out on your computer at all. Git there
makes the branch and the worktree's entry in `.git`, and the files are checked out in a one-off
container from your box image, with no network. What that means for you:

- **It needs the box image**, with `git` and `sh` in it (any image a box runs in has both). Before
  your first `fy box build`, `fy worktree add` stops with the command to run first, and has changed
  nothing.
- **Git LFS files arrive as pointers.** Run `git lfs pull` in the worktree's box to fetch them;
  `fy worktree add` says so when the repo uses LFS.
- **The first `git status` on your computer in a new worktree is slower** while git re-reads each
  file once (the checkout's index was written in the VM); after that it's as fast as anywhere.
- **If the checkout fails, nothing is kept**: the worktree and the branch `fy worktree add` made are
  removed, and it prints the command to run again.

Comparing files runs them too: to tell whether a file changed, git reads it through its `clean`
filter. So `fy worktree remove` doesn't ask git on your computer whether a worktree has uncommitted
work, or have it save that work. Both happen in the same kind of container, and git on your computer
then saves the result on a ref and deletes the checkout.

## Worktrees

For parallel work, make a worktree on your computer with `fy worktree add <name>`: its own checkout
and branch (`wt/<name>` by default, forked from the trunk), its own box, stack, mode and ports, all
in the one VM and the one project: its name, state and VM are main's, whatever its own
`foldyard.toml` says
([configuration](./configuration.md#the-adopted-config-what-the-host-actually-runs)). Run in a box,
`fy worktree add` makes one with no stack of its own: see
[worktrees a box makes](#worktrees-a-box-makes).

A worktree made with plain `git worktree add` outside the worktrees root isn't an fy worktree, and
neither is one Claude Code makes under `.claude/worktrees/`. You can go on using one with git, but
fy doesn't act in it: on your computer every `fy` project verb run there refuses and names your main
checkout, and `fy worktree add` won't make a worktree on a branch one has checked out. The one
exception is `fy box exec` in a worktree inside your main checkout, as Claude Code's are: it runs in
main's box, which sees that path as it is, so the git hooks a commit there runs still check it in
the box (with `--skip-if-down`, a down box skips them as ever). Main's box is the one it runs in,
whatever `WORKTREE` or `FOLDYARD_REPO` says. To bring one under fy, move it into the worktrees root
(`git worktree move`) and run `fy worktree add <name>`, which registers it after asking.

Your computer tells which project, and which checkout of it, a directory belongs to by its own
records, never by what git or a `foldyard.toml` there says: the box can write those. The records are
the main checkouts you adopted, each one's registered worktrees, and each one's worktrees root where
that root is the project's own. Walking up from where `fy` runs, the nearest one decides; anything
the box wrote in between only makes fy refuse. So a registered worktree whose `.git` is missing,
emptied or pointing elsewhere is refused (`git worktree repair <path>`, run in main, mends it), and
so is a repository made inside a checkout, the worktrees root itself, a directory two projects'
roots both claim until one registers it, and a path you reached through a link that leads somewhere
the path doesn't say. `WORKTREE=<name>` only picks which registered worktree to act on; it doesn't
skip any of this.

Your computer runs each checkout's *adopted* `foldyard.toml`, never the working tree
([configuration](./configuration.md#the-adopted-config-what-the-host-actually-runs)), so
`fy worktree add` settles the new checkout's adoption before it finishes. A worktree whose config is
byte-identical to the one adopted for main is adopted without a prompt, since it grants nothing main
hasn't already been granted: it says `✓ config adopted — identical to main's`. One that differs is
shown as a diff against main and adopted only if you answer the prompt; with no terminal, or if you
decline, the worktree is still made, nothing is adopted, and `WORKTREE=<name> fy config diff` then
`WORKTREE=<name> fy config adopt` (or the prompt at `WORKTREE=<name> fy box up`) finishes it. `-y`
only answers the question about registering an existing worktree; it never adopts a config. When a
worktree's config is adopted for the first time, main's keyless `claude`/`codex` modes are copied
into the worktree, so its box can use them from the start. That happens wherever the first adoption
is: at `fy worktree add`, at the prompt from `WORKTREE=<name> fy box up` or `fy up`, from
`WORKTREE=<name> fy config adopt`, or in the TUI. Only modes main has on are copied, never one the
worktree's mode already sets, and a later adoption copies nothing.

`fy worktree add <name> --box` then brings the worktree's box up, the same as running
`WORKTREE=<name> fy box up`. If its config wasn't adopted it doesn't start the box, and says so.

Every box sees every checkout: main's box reaches the worktrees, a worktree's box reaches main and
the other worktrees. Edit and read across them freely, but run installs in a checkout from its own
box. Each box keeps only its own checkout's `[box].shadow_volumes` dirs in volumes; the same dirs of
every other checkout are read-only in it, so `pnpm install` or `uv sync` there fails instead of
writing onto your computer's disk. The note in each of those dirs, `RUN-IT-IN-ITS-OWN-BOX`, gives
the command: from that directory, `WORKTREE=<name> fy box exec '<command>'` runs it in the
worktree's own box (`WORKTREE= fy box exec '<command>'` in main's), as long as that box is running —
`WORKTREE=<name> fy box up` on your computer starts it. A box's mounts are set when it's created, so
a worktree added while a box runs isn't in them: `fy worktree add` adds its masks to each running
box instead, without recreating it ("masked '<name>' in <box> without recreating it"), and
`fy box up` on a running box adds any it lacks (after a restart of the box or the VM, which drops
them). Where it can't — a box under the gVisor posture, a rootful engine in the VM, a kernel older
than Linux 5.12, or a dependency dir that's a symlink — it names the box and says why, and
recreating that box masks it; `fy doctor`'s `box shadowing` row reports a box that still lacks one.
Only worktrees registered with your computer are masked: a
[worktree a box made](#worktrees-a-box-makes) has volumes instead, and one made with plain
`git worktree add` is masked in no box, recreated or not, until `fy worktree add <name>` on your
computer registers it (it asks first). The row lists those apart, with that fix.

`fy worktree remove <name>` takes down that worktree's box, stack and volumes, deletes the checkout
and its adopted config, and keeps its branch, so a worktree added later under the same name starts
with nothing adopted. A worktree with uncommitted work needs `--force`; that work is saved first on
a ref in the main checkout (`refs/fy/removed/<name>-<time>`), and the command prints how to restore
it. A worktree that holds another one, such as one an agent's own worktree tool made under
`.claude/worktrees/` inside it, is refused before anything is taken down: deleting it would delete
the one inside, uncommitted work and all, which neither git nor that save would notice. Remove the
inner one first, from a box, with `git worktree remove <path>`; `--discard` removes both anyway, and
git then lists the inner one as prunable until `git worktree prune`.

Uncommitted work here means files that differ from the worktree's last commit once everything is
unstaged: changed, deleted or untracked files that aren't gitignored, and changes inside a
submodule. What's staged doesn't count. So a stale stage, such as the one your computer's side of a
worktree shows after commits made in its box, doesn't stop a plain remove. Two things are not
protected:

- **Gitignored files** are neither counted nor saved.
- **Changes that exist only in the stage** are lost: a change you staged and then undid in the file
  itself. Unstaging everything would discard them too, and the save keeps files, not the stage.

The check for uncommitted work, and the save, run in a container from your box image (see above), so
`fy worktree remove` needs the image and the VM. Without them it stops before it has taken anything
down, and says what to run first. If you can't provide them and you're sure nothing uncommitted in
the worktree matters, `--discard` removes it without checking or saving anything (a worktree with
submodules still needs `--force` as well, as without `--discard`). If the check or the save fails,
the worktree is kept.

### Worktrees a box makes

A change that needs no stack of its own (a fix, a doc edit, a review branch, unit tests) doesn't
need its own box. Run in a box, `fy worktree add` makes a worktree with nobody on your computer:

```bash
fy worktree add <name> [<branch>]       # branch: wt/<name>, off the default branch, untracked
# … edit, install, test, commit …
fy worktree remove <name>               # in the box: its checkout and volumes; the branch stays
```

Uncommitted work keeps it, as on your computer: `--force` saves that on `refs/fy/removed/` first.

In a box, `fy worktree remove` takes one that a box made, and also one made with plain
`git worktree add` (before boxes could make their own, say), as long as it's in the worktrees root
and your computer never registered it. It refuses a registered one, which has a box, a stack and
adopted config on your computer, and says to run the command there. The box knows which is which
from a list your computer publishes in the [to-box](./configuration.md#what-the-vm-mounts); with no
list to read, it refuses a plain one too and says so. `fy worktree list` in the box shows each:
`(made in a box)`, `(not registered)` or `(registered on your computer)`.

It is an ordinary git worktree in the worktrees root, beside those made on your computer. Your
computer sees the files, the branch and its commits at once. Its dependency dirs
(`[box].shadow_volumes`) are volumes in the box that made it, so an install runs at VM-disk speed
and those dirs stay off your computer's disk (whatever else it writes, a lockfile say, is in the
checkout). They are mounted before the repo's post-checkout hook runs, so a hook that installs is
covered too.

The volumes go into the running box through a one-off container that shares only the box's own
processes and has three capabilities and no network; foldyard prints the command. Nothing runs on
your computer. A restart of the box or the VM drops the mounts and keeps the volumes: the box mounts
its own again as it starts, and so do `fy box up` and `fy worktree add <name>`. Until then each dir
is read-only and empty, which stops npm, uv and pnpm's hoisted linker from installing onto your
computer's disk. It is a safety net only: pnpm's isolated linker replaces the dir, and the box's
root can. Under the gVisor posture nothing can be mounted, so it stays that way: edit and commit
there, and to install, ask for `fy worktree add <name> --box` on your computer and install in the
box it makes.

What it doesn't have is a stack, ports, a mode or adopted config. Commands in it run in the box that
made it, under that box's mode, and `fy up` there is refused. To give it those, run the same command
on your computer, which registers it: `fy worktree add <name> --box`. Do it with `--box`, or say yes
when it offers to start its box: once it's registered, its dependency dirs are read-only in every
other box, so a session that was using it there can edit and commit but not install or run its tools
(a git hook that needs them is skipped or fails). It goes on in the worktree's own box:
`WORKTREE=<name> fy box exec '<command>'` from any box, or `WORKTREE=<name> fy box shell`. Its new
box finds the dependency volumes as they are, and the box that made it then sees them read-only,
like any other registered worktree's. Registering doesn't run `[project].worktree_init`:
`fy worktree init <name>` does.

The box's git (foldyard's shim) says so if you reach for plain git instead. `git worktree add`
outside the checkout, and `git worktree prune`, are refused with the command to use; one inside the
checkout, where Claude Code's own worktree tool puts its (`.claude/worktrees/`, which `fy init`
ignores), is made with a warning not to install there. That is a nudge, not a boundary:
`FY_GIT_SHIM_OFF=1 git …` runs it anyway, and `[box] git_index_split = false` has no shim.

## What `fy doctor` checks

- **`git shim`** (in the box) — the `git` on PATH is foldyard's shim. If it isn't, the image put
  another git first on PATH or the install failed, and none of the above protects you. A new box
  runs this check once at the end of `fy box up`.
- **`git shim hooks`** (in the box) — the hooks the shim uses to check branch moves are installed,
  and only root can change them. The fix for either row: `fy box recreate` on your computer.
- **`shared git config`** (both sides) — `.git/config` still has git's own keys and a remote. A
  config file emptied by a write race fails here, with the keys to restore; no remote is a warning,
  since `fy verify` then can't prove the push refusal.
- **`leftover autostash (<checkout>)`** (your computer) — the stuck `git pull --rebase` above, with
  its recovery commands.
