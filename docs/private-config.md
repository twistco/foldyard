# Foldyard on a repo that doesn't use it

Contributing to someone else's project, you may want to work in a foldyard box while the project
itself has no use for foldyard. Committing a `foldyard.toml` there isn't an option, and carrying it
on a branch you rebase and keep out of every pull request gets old fast. `fy init --private` sets
foldyard up **untracked and hidden from git** in your clone only, so `git status`, your commits and
your pull requests never show it, while foldyard works as it would in a project that ships it.

It works because your computer never runs the `foldyard.toml` in the checkout anyway. It runs the
copy you **adopted**, kept in `~/.foldyard/` (see [configuration.md](./configuration.md) and
[ADR-0022](./adrs/0022-host-runs-the-adopted-config.md)), and nothing in foldyard cares whether git
tracks the file. [ADR-0037](./adrs/0037-a-private-setup-on-a-project-that-doesnt-use-foldyard.md)
has the reasoning.

## Setting it up

From a clean tree (`git status` shows nothing), at the top of your clone's main checkout:

```bash
fy init --private --with claude
fy config diff     # what your computer would start running
fy config adopt
fy box up
```

Size the VM in the first command if the project needs more than the default 4 CPUs, 8 GiB of memory
and 60 GiB of disk: add `--cpus`, `--memory` (MiB) and `--disk` (GiB). A heavy build (a C++
dependency built with Bazel, say) wants twice that.

`fy init --private` writes the usual `foldyard.toml`, with `private = true` under `[project]`, and
the foldyard skill. Unlike plain `fy init`, it:

- puts foldyard's ignore block, plus the private files themselves, into `.git/info/exclude`, and
  leaves `.gitignore` alone. `.git/info/exclude` is git's own ignore list for one clone: it works
  like `.gitignore`, but is never committed or pushed, and covers the main checkout and every
  worktree;
- writes no `foldyard.local.toml.example`, a template for a team;
- refuses a clone where git already tracks a foldyard file (see [Already set up?](#already-set-up)).

| file | in this setup |
| -- | -- |
| `foldyard.toml` | yours, untracked |
| `foldyard.local.toml` | optional: everything can go in `foldyard.toml`, since all of it is yours |
| `.claude/skills/foldyard/` | untracked |
| `.devbox-*/`, `.dev-mode.json` | written at run time, untracked |

All of it is hidden by `.git/info/exclude`, and `fy doctor` checks that git still sees none of it.

Adopting before `fy box up` keeps the review apart from a command that goes on to build a VM, though
`fy box up` would also offer the file for adoption on its own.

## Already set up?

**The usual way, nothing committed** (`git status` lists `foldyard.toml` and the rest as untracked,
and `.gitignore` as modified): run `fy init --private`. It keeps your `foldyard.toml`, adding
`private = true`, takes foldyard's block back out of `.gitignore` (deleting the file if `fy init`
created it), removes the untracked `foldyard.local.toml.example`, and writes the exclude list. Then
`fy config diff` shows the one added line, and `fy config adopt`. The box you have carries on.

**By hand, from an earlier version of this page** (a `.fy/worktree-init.sh` script, a pasted
paragraph in `system_prompt`): run `fy init --private` too. Worktrees now get their copy from
foldyard, and the agent's note comes from foldyard, so `[project].worktree_init`, the `.fy/` script
and the paragraph can go, in the same adopt.

**Some of it committed**, on a branch of yours or in `.gitignore`: `fy init --private` refuses. Take
it out of the history before anything is pushed; a commit that removes it would still carry it into
a pull request. Copy `foldyard.toml` and `.claude/skills/foldyard/` aside first, since git deletes a
file from the working tree when a rebase or checkout drops the commit that tracked it. Then drop or
amend those commits, put the files back, and run `fy init --private`. If they have already reached
upstream, the clone's setup is no longer private, and this page can't make it so.

## Worktrees

`git worktree add` doesn't bring untracked files along, so foldyard does. `fy worktree add <name>`
writes main's **adopted** `foldyard.toml` (and `foldyard.local.toml`, if you have one) and the
foldyard skill into the new worktree, from your computer. Being exactly the config you reviewed for
main, it is then adopted without a prompt. Main's working tree is never copied: the box can write
it, and an edit there that you haven't adopted isn't something to hand on.

Changing the config later:

```bash
fy config diff && fy config adopt          # main first
fy worktree init <name>                    # writes main's adopted config into the worktree again
WORKTREE=<name> fy config diff             # then the worktree's own review
WORKTREE=<name> fy config adopt
```

An edit under `[box]` (packages, tools, shadow volumes, caches) also needs each box recreated after
the adopt, since the box installs them only when it's created: `fy box down && fy box up`, and
`WORKTREE=<name>` in front of both for a worktree's box.

`[project].worktree_init` still runs after the copy, for the project's own untracked files (an
`.env`, say). `fy worktree remove <name>` doesn't count the private files as uncommitted work: they
are copies of main's. It keeps the worktree's branch (`wt/<name>` unless you named one); delete it
with `git branch -d` when you're done with it.

## Telling the agent

The agent in the box sees `foldyard.toml` as an ordinary file and has no way to know it's private,
so in a private setup foldyard adds a note to `[claude]`'s and `[codex]`'s system prompts. The note
says:

- the files are untracked on purpose, and hidden by `.git/info/exclude`;
- never commit them, and keep foldyard out of tracked files, commit messages and PR text;
- don't run what deletes ignored files;
- ask before changing `foldyard.toml`, which does nothing until you adopt it.

It's foldyard's own text, so it follows the version you have installed, and it reaches the next
`fy claude` or `fy codex` session after you adopt `private = true`. The bundled `foldyard` skill
also knows the setup exists, for a session started some other way.

## What can lose the files

Git treats ignored files as expendable. The ones to know:

- **`git clean -x` or `-X`** deletes them, and **`git stash --all`** stashes them away.
- **A branch that tracks a `foldyard.toml`** (if upstream ever adopts foldyard, say): switching to
  it overwrites yours **without a warning**, and switching back deletes it.

Your computer's adopted copy is unaffected by either: the files in `~/.foldyard/` aren't in the
checkout, and the host keeps running what you adopted. That copy is also how you get them back:

```bash
FOLDYARD_REPO="$PWD" fy config revert   # rewrites foldyard.toml from the adopted copy
fy skill install foldyard               # the skill, if it went too
```

`FOLDYARD_REPO` is needed because, with the file gone, `fy` no longer recognises the directory as a
project.

## Limits

- **Per clone.** `.git/info/exclude` isn't pushed, so a fresh clone needs `fy init --private` again
  (the adopted copy is per checkout path too). Keep your `foldyard.toml` somewhere of your own if
  you'll want it again.
- **The box can still edit the file.** It's in the checkout, so it's as writable from the box as in
  any project. That's fine for the same reason as everywhere else: your computer runs only what you
  adopt, and `fy config status` reports any difference.
