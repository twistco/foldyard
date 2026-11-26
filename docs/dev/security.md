# What the box can reach on your computer

The threat model a user reads is [security](../security.md); the reasons are in the ADRs
([0022](../adrs/0022-host-runs-the-adopted-config.md): the host runs the adopted config,
[0023](../adrs/0023-no-host-executed-code-from-the-repo-mount.md): no host-executed code from the
repo mount). This page holds the rules for code on your computer that touches anything a box wrote.
Read it when you implement or review a change that prints, logs or shows text, reads a file in a
checkout, or runs a child process on your computer.

Assume the box is hostile, and that whatever it can write was written to hurt you.

## What the box can write

- **The checkout**: `foldyard.toml`, the compose files, and `.git` (its config, hooks, refs, the
  `.git/worktrees/<id>/` admin dirs, and each worktree's `.git` file).
- **The worktrees root**: any directory, with a `.git` file in it that names any gitdir. Your
  computer keeps its own list of the worktrees it registered (`worktree_registry`), and anything
  else under that root is the box's.
- **What runs on its engine**: containers with any label values it likes. podman checks the names of
  containers, volumes and networks, but anyone on the engine can set a label to anything, including
  the compose labels foldyard reads. And everything a container, a build or the stack prints.
- **The guest monitor's records**: a container's name and labels there are what its creator claimed,
  and so are the event fields (`monitorlog`).
- **Through the proxy**: request paths, User-Agents and bodies. mitmproxy checks the hostname, not
  the path.
- **Its agents' transcripts**, including the folder names they are filed under.

## Text the box chose reaches your terminal inert

A string the box chose may carry ESC sequences: an OSC 52 write to your clipboard, cursor moves that
rewrite the `allow X?` line of a consent prompt, line erases. It may carry C1 controls (U+0080–009F,
which `tomllib` accepts in a string) or bidi overrides. In the TUI, Textual markup such as
`[@click=app.screenshot('x','/path')]` writes a file when clicked. So every copy the host prints,
logs or shows goes through one of these:

| Where it goes | Use |
| -- | -- |
| One value on a line: a config string, a name, a path | `term.printable` (`monitorlog.printable` is the same rule): each non-printable shown as its escape, so it stays visible |
| Text over several lines: a log tail, a command's output | `term.printable_lines`: the same, keeping `\n` and `\t` |
| A child process whose output a box can shape | `guestcompose.stream_here` / `relay` on your computer, `stream` / `stream_to` / `run_relayed` from the VM: each line through `guestcompose.printable`, which strips ANSI and drops the rest. One such line on its own: `guestcompose.show` |
| A log file read back (`fy host logs`) | `supervisor._inert` |
| A Textual widget | `tui._content(...)`: `Content` built from plain parts, never parsed as markup. Toasts go through `DevModeTui._toast` (`markup=False`) |
| Rich markup: a `DataTable` `str` cell, a plugin panel | `tui._shown` (`escape` of the printable text) for each piece the box chose. A panel's markup has its pieces escaped by the plugin (`PanelData`), and `tui._panel_markup` makes the whole inert |

`agents._printable` and `allowlist._printable` drop non-printables and cut the text short, where the
rest show them as escapes. They are for lists a person scans, where a long or odd entry is noise;
anything a person might need to read exactly uses `printable`.

- **`rich.markup.escape` isn't enough for Textual markup.** Textual's parser reads markup that
  `escape` leaves alone (`[ @click=…]`, a `[` inside a tag). Box text goes into a Textual widget
  (`Static`, `.update`, a label, `notify`) as `Content`, never as an escaped markup string. Rich
  parses a `DataTable`'s `str` cell, and Rich's parser is the one `escape` was made for, so `_shown`
  is enough there; a `Text` or `Content` cell needs nothing.
- **Inherit stdio only where the user asked for a terminal**: `fy shell`, `fy box shell`,
  `fy agent attach`, and the compose verbs in `stack._INTERACTIVE`. Anything else that runs on your
  computer and prints what the box shaped (compose, a build, a checkout container) is relayed.
  `fy box exec` still inherits stdio, and the git hooks run it during a `git commit` on your
  computer, so what a hook prints there reaches the terminal raw: a known gap.
- **Escape only the copy you print, never the data.** A hostname the proxy matches, a path that is
  opened and a branch that is checked out stay as they were.
- **Escape display-only config strings when they are parsed** (a `why`, a `label`, a `how`). Refuse
  an identifier that isn't printable (a switch name, a host, a header) with a `ValueError` that
  shows its `repr`: someone would type a name you showed them escaped, and that name wouldn't match.
- **Test with the hostile string itself**: an ESC sequence, `\x9b`, U+202E and a `[@click=…]` name,
  asserting what reaches the screen. A TUI test checks the rendered text, not the markup that was
  meant.

## Acting on what the box wrote

- **Config**: the host reads the adopted copy ([invariants](invariants.md)). A file that doesn't
  parse is never adopted, not even as a comment-only edit (`configpin.Unparseable`).
- **Git on main or a registered checkout**: `.git/config` and the hooks are the box's, and git reads
  them. Git that can check out, update a ref or refresh the index goes through `worktree._host_git`:
  no hooks, no fsmonitor, no fetch, and no run at all when the config can't be scanned for hooks.
  Git that only reads goes through `worktree._git_read`. A worktree's `.git` file is the box's too,
  even for one your computer registered.
- **A checkout your computer didn't register**: nothing runs in it, git included (not even
  `rev-parse`), since its `.git` file can name a gitdir, and with it a config, that the box built.
  Read what you need the way `worktree._branch_read` does:
  - Build the path from parts the host knows (`worktree._own_admin_dir`).
  - Open it with `O_NOFOLLOW | O_NONBLOCK`, check `S_ISREG`, and cap the size.
  - Only return a value of the shape you expect.
  - Name it from main's adopted config (`config.stray_identity`), not from the directory.
- **Paths in general**: never follow a link the box could have placed. Compare `realpath`s before
  trusting that something is where it says it is.
- **Commands**: none from config, and none run from the mount (ADR-0023). A consumer's script runs
  in a container.

## Text the box wrote that an agent on your computer reads

By default, the box's Claude sessions are archived into your computer's Claude history, under the
checkout's own folder (`transcripts`). So `claude --resume`, `claude --continue` and `/resume` on
your computer offer them, and `--continue` picks the latest without asking. Resuming one puts text
the box wrote into an agent that has your computer's access. The `fy claude` refusal on your
computer warns against it. Codex sessions go to a Codex home of foldyard's own, never to `~/.codex`.

Repo text that steers the in-box agent (`[claude].system_prompt`, `[codex].system_prompt`,
`[claude.settings]`, `[codex.config]`) is listed by `fy config widenings`.

## Reviewing a change

1. Does a new print, log line, notification, prompt or widget show a string that came from anything
   in [What the box can write](#what-the-box-can-write)? Which helper does it go through?
2. Is the sink Textual? Then the text goes in as `Content` or with `markup=False`, not through
   `escape`.
3. Does a child process's output reach the terminal? Then it is relayed, unless the verb is a
   terminal by design.
4. Does code on your computer open a path, or run git, under something the box can write?
5. Does a decision on your computer read the tree's config instead of the adopted copy?
6. Does a consent prompt put box-chosen text beside the thing being consented to?
