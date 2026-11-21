# ADR-0037 — A private setup on a project that doesn't use foldyard

- **Status:** Accepted (2026-11-20). Built: `fy init --private`, `[project] private`, each
  worktree's copy written from your computer, the agent's note and doctor's row.
- **Sources:** [private-config.md](../private-config.md), which documented the setup as manual steps
  before this; [ADR-0022](./0022-host-runs-the-adopted-config.md) (the host runs the adopted
  config); [ADR-0023](./0023-no-host-executed-code-from-the-repo-mount.md) (no host code from the
  mount); `mountwrite` (host writes that never follow a link the box planted).

## Context

Contributing to a project that doesn't use foldyard, you can still work in a foldyard box: your
computer runs the config you adopted, kept outside the checkout, and nothing in foldyard needs git
to track `foldyard.toml`. What needed care was git: keeping foldyard's files out of every commit and
pull request, and getting them into new worktrees, which `git worktree add` doesn't do for untracked
files.

The documented setup did it by hand in five steps:

- a `sed` that moves `fy init`'s ignore block from `.gitignore` into `.git/info/exclude`;
- undoing `fy init`'s other edits;
- a worktree script written into `.fy/` and named by `[project].worktree_init`;
- a paragraph pasted into each agent's `system_prompt`.

The script lived in the checkout, so the box could rewrite it. That granted nothing new: it runs in
a container in the VM with the same mounts the box already has, and a worktree's config is adopted
without a prompt only when it is byte-for-byte main's adopted copy. But it was a box-writable file
doing a job foldyard could do itself, with no copy anywhere if it was lost. The pasted paragraph
drifted from what the installed foldyard knew.

## Decision

1. **`[project] private = true`** declares the setup. Like every other key, what your computer acts
   on is the adopted copy, so the box can't switch it on or off by editing the file.

2. **`fy init --private`** does the setup:

   - It refuses a directory that isn't a clone's main checkout, and a clone where git tracks
     `foldyard.toml`, `foldyard.local.toml` or the skill. A tracked file needs its history fixed
     first, and only you can decide that.
   - It writes foldyard's fenced ignore block, plus the private files themselves, into the clone's
     own `.git/info/exclude`, and leaves `.gitignore` alone.
   - It writes no `foldyard.local.toml.example`, since that template is for a team.
   - On a clone set up the usual way, it converts: the existing `foldyard.toml` is kept with
     `private = true` added, foldyard's block comes back out of `.gitignore`, and an untracked
     template goes.

3. **A worktree's private files come from your computer.** `fy worktree add` and `fy worktree init`
   write two things into the worktree:

   - main's **adopted** `foldyard.toml` and `foldyard.local.toml`, never main's working tree, which
     the box can write;
   - the bundled foldyard skill.

   Every write goes through `mountwrite`, so a link the box planted is replaced, never written
   through. The writes are rooted at the worktrees root, not at the worktree's own directory: the
   box can write the worktrees root, so the worktree's directory may itself be a link. Main's
   working tree isn't read at all. `worktree_init`, as before, runs only in a container in the VM.
   Being main's adopted bytes, the config is then adopted without a prompt by the existing rule.
   `[project].worktree_init` still runs afterwards, for a project's own untracked files.

4. **The agent is told by foldyard.** In a private setup, `[claude]`'s and `[codex]`'s system
   prompts get a fixed note: the files are untracked on purpose, what not to run, and to ask before
   changing the config. It's foldyard's own text, so it follows the installed version.

5. **The skill goes in through `mountwrite` too**, for `fy init` and `fy skill install` alike: a
   `.claude` link in the checkout is refused, and `--force` overwrites files rather than deleting a
   directory first.

6. **Doctor checks it.** A tracked private file fails (the next push carries it). A file git would
   show, or foldyard's block back in `.gitignore`, warns.

Git on your computer runs only `ls-files` and `check-ignore`, through the same hookless,
fsmonitor-less, fetch-less wrapper as `fy worktree add`. Neither command checks a file out or hashes
one, so no filter driver runs.

## Consequences

- The setup is one command, and the manual steps, the `sed` and the script are gone from the docs. A
  clone set up by hand keeps working: `fy init --private` marks it private, after which its
  `worktree_init` script is redundant and can go.
- A worktree's config now comes from main's adopted copy, not main's working tree. An edit in main
  that you haven't adopted no longer reaches new worktrees. Before, it did, and was then shown for
  review there.
- The exclude list is per clone and isn't pushed, as before: a fresh clone needs `fy init --private`
  again.
