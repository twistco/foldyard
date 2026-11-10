# Foldyard on a repo that doesn't use it

Contributing to someone else's project, you may want to work in a foldyard box while the project
itself has no use for foldyard. Committing a `foldyard.toml` there isn't an option, and carrying it
on a branch you rebase and keep out of every pull request gets old fast. This page sets the same
files up **untracked and hidden from git** in your clone only, so `git status`, your commits and
your pull requests never show them, while foldyard works as it would in a project that ships them.

It works because your computer never runs the `foldyard.toml` in the checkout anyway. It runs the
copy you **adopted**, kept in `~/.foldyard/` (see [configuration.md](./configuration.md) and
[ADR-0022](./adrs/0022-host-runs-the-adopted-config.md)), and nothing in foldyard cares whether git
tracks the file. What needs care is git: hiding the files without editing a tracked file, getting
them into new worktrees, and the few git commands that delete what's ignored.

## What foldyard puts in a checkout

| file | written by | in this setup |
| --- | --- | --- |
| `foldyard.toml` | `fy init` | yours, untracked |
| `foldyard.local.toml.example` | `fy init` | deleted: it's a template for a team |
| `.gitignore` (a fenced block) | `fy init` | the block moves to `.git/info/exclude` |
| `.claude/skills/foldyard/` | `fy init` | untracked |
| `.devbox-*/`, `.dev-mode.json` | the box and your computer, at run time | excluded, by the moved block |
| `.fy/worktree-init.sh` | you, below | untracked |

`.git/info/exclude` is git's own ignore list for one clone. It works like `.gitignore`, but it is
never committed or pushed. It lives in the repository's shared git directory, so one list covers
the main checkout and every worktree.

## Setting it up, in order

The order matters in two places. The files are hidden **before** anything else can see them, so
an agent's `git add -A` never stages them. And everything goes into `foldyard.toml` **before** you
first adopt it and create the box, so you review the whole file once and the box is created with
the agent already configured.

Start from a clean tree (`git status` shows nothing), at the top of your clone:

1. **Scaffold.**

   ```bash
   fy init --with claude
   ```

2. **Move the ignore block into the exclude list, and undo `fy init`'s other edits.**

   ```bash
   exclude="$(git rev-parse --git-common-dir)/info/exclude"
   sed -n '/^# >>> foldyard/,/^# <<< foldyard/p' .gitignore >> "$exclude"
   git restore .gitignore 2>/dev/null || rm .gitignore  # tracked: undo the edit; else init made it
   rm foldyard.local.toml.example
   printf '%s\n' /foldyard.toml /.claude/skills/foldyard/ /.fy/ >> "$exclude"
   git status --short   # must print nothing
   ```

   The block is copied from what `fy init` wrote, rather than listed here, so it stays whatever
   your installed foldyard generates. If `git status` still shows something, add it to the same
   file before going on.

3. **Write the worktree script** (see [Worktrees](#worktrees) for what it's for):

   ```bash
   mkdir -p .fy
   cat > .fy/worktree-init.sh <<'EOF'
   #!/bin/sh
   # Copies this clone's private foldyard files into a new worktree. `fy worktree add` runs it in
   # a container in the VM, from the new worktree, as: sh <this> --source <main checkout>
   set -eu
   [ "${1:-}" = --source ] && [ -n "${2:-}" ] || { echo "usage: $0 --source <main>" >&2; exit 2; }
   src=$2
   cp "$src/foldyard.toml" ./foldyard.toml
   if [ -f "$src/foldyard.local.toml" ]; then cp "$src/foldyard.local.toml" ./foldyard.local.toml; fi
   mkdir -p .claude/skills
   rm -rf .claude/skills/foldyard
   cp -R "$src/.claude/skills/foldyard" .claude/skills/foldyard
   EOF
   ```

4. **Edit `foldyard.toml`.** Two additions beyond whatever your project needs:
   - under `[project]`, `worktree_init = ".fy/worktree-init.sh"`;
   - at the end of `[claude]`'s `system_prompt`, before its closing `"""`, the paragraph in
     [Telling the agent](#telling-the-agent). (If you use Codex too, add it to `[codex]`'s.)

5. **Review and adopt it**, then create the box:

   ```bash
   fy config diff     # what your computer would start running
   fy config adopt
   fy box up
   ```

   `fy box up` would also offer the file for adoption on its own. Doing it as separate steps
   keeps the review apart from a command that goes on to build a VM.

## Worktrees

`git worktree add` doesn't bring untracked files along, so a new worktree would start with no
`foldyard.toml`, and its box with an empty config. The script from step 3 fixes that:
`fy worktree add <name>` runs `[project].worktree_init` in a container right after the checkout,
and then offers the worktree's config for adoption. When main's file matches what you adopted
there, the copy is byte-for-byte main's adopted config and is adopted without a prompt: it grants
nothing main hasn't been granted. (If main has an edit you haven't adopted, you get the usual
review instead.)

Changing the config later, after an edit in the main checkout:

```bash
fy config diff && fy config adopt          # main first
fy worktree init <name>                    # copies it into the worktree again
WORKTREE=<name> fy config diff             # then the worktree's own review
WORKTREE=<name> fy config adopt
```

## Telling the agent

The agent in the box sees `foldyard.toml` as an ordinary file and has no way to know it's private,
so tell it. This paragraph goes into `system_prompt` (step 4). It reaches the next `fy claude`
session after you adopt it:

```text
This checkout's foldyard setup is private to the user: the upstream project doesn't use
foldyard. foldyard.toml, .fy/ and .claude/skills/foldyard/ are untracked on purpose and
hidden by .git/info/exclude, not by .gitignore.
- Never commit them (no `git add -f`), never add foldyard entries to .gitignore or any
  other tracked file, and keep foldyard out of commit messages, code and PR text.
- Don't run `git clean` with -x or -X, or `git stash --all`: they delete or take away
  the ignored files.
- Changing foldyard.toml: ask the user first, saying what and why. An edit does nothing
  until the user reviews it with `fy config diff` and runs `fy config adopt` on their
  computer, so tell them when you've made one.
- `fy docs private-config` explains the setup.
```

The bundled `foldyard` skill also knows the setup exists, for a session started without the
paragraph: it checks whether git tracks `foldyard.toml` before treating it as the project's.

## What can lose the files

Git treats ignored files as expendable. The ones to know:

- **`git clean -x` or `-X`** deletes them, and **`git stash --all`** stashes them away.
- **A branch that tracks a `foldyard.toml`** (if upstream ever adopts foldyard, say): switching
  to it overwrites yours **without a warning**, and switching back deletes it.

Your computer's adopted copy is unaffected by either: the files in `~/.foldyard/` aren't in the
checkout, and the host keeps running what you adopted. That copy is also how you get the file
back:

```bash
FOLDYARD_REPO="$PWD" fy config revert   # rewrites foldyard.toml from the adopted copy
fy skill install foldyard               # the skill, if it went too
```

`FOLDYARD_REPO` is needed because, with the file gone, `fy` no longer recognises the directory as
a project. `.fy/worktree-init.sh` has no copy anywhere; recreate it from step 3.

## Limits

- **Per clone.** `.git/info/exclude` isn't pushed, so a fresh clone needs these steps again (the
  adopted copy is per checkout path too). Keep your `foldyard.toml` somewhere of your own if
  you'll want it again.
- **No team template.** The `foldyard.local.toml` overlay is for splitting shared config from
  personal config. Here all of it is personal, so put everything in `foldyard.toml`.
- **The box can still edit the file.** It's in the checkout, so it's as writable from the box as
  in any project. That's fine for the same reason as everywhere else: your computer runs only
  what you adopt, and `fy config status` reports any difference.
