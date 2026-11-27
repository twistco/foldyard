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
| A child's output into a log file (an image build's) | `buildlog.BuildLog.line`: each line through `guestcompose.printable`, then stamped, so `cat` on the file later plays none of it |
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
  computer and prints what the box shaped (compose, a build, a checkout container, `fy box exec`,
  which the git hooks run during a `git commit` on your computer) is relayed. `fy box exec` cleans
  only what goes to a terminal: piped or redirected, its output is data and passes byte for byte. So
  a hook that captures it and prints it itself
  (`out=$(fy box exec 'pnpm lint') || { echo "$out"; exit 1; }`) puts the raw bytes on your
  terminal, since fy's own output there isn't a terminal. A hook that lets fy write to the terminal
  itself is cleaned (lefthook gives it a pty; git runs hooks with stdout on stderr). A process
  `guestcompose.stream_here` relays gets neither your stdin nor your Ctrl-C: it runs in a process
  group of its own, and foldyard stops relaying before it stops the group (on Ctrl-C, and on a
  SIGHUP, SIGTERM or SIGQUIT that ends foldyard), so what the process says about an interrupt (a
  traceback) never reaches you. That stops what it started on your computer only: a command
  `podman exec` started in a container runs on. The other relays (`run_relayed` and `stream_to` on
  the VM path, `devmode.run_stream`) keep the terminal's process group unless they are given an
  `on_spawn`.
- **Colour on relayed text is foldyard's, from what the text says.** The box's own escapes are
  stripped, since coloured text could draw a `✓` line that looks like foldyard's. A relay that
  colours (`guestcompose.compose_style`, through `term.compose_line`) adds codes from a fixed set
  only, after cleaning, to the service prefix, the level words and a stamp, closes each span, and
  never paints a glyph. Only on a terminal (`term.color_enabled`), never into a file or a pipe.
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
- **Reading a file the box can write**: open it non-blocking, require a regular file, and cap its
  size, checking the size as it's read too: `mountwrite.read_regular` (`mountwrite.read_opened` on
  an fd you opened yourself, as `githeal._read_at` does). A plain `read_text` there lets the box
  hang the process on your computer: a FIFO with no writer blocks the `open` itself (one planted as
  a worktree's `.git` file stopped the supervisor's tick, and a posture's expiry with it), and one
  it keeps writing to (or a link to `/dev/zero`) reads without end. A link to a regular file is
  still followed where it always was (`foldyard.toml`), and one to `/dev/null` reads as empty. A
  refused file reads like an unreadable one, and its reason is said. Where that would change a
  decision, refuse it and don't offer it, as adoption does (`configpin`'s `Drift.unadoptable`).
- **A child that reads what the box wrote**: no compose client runs on your computer
  (`guestcompose`; `stack._in_the_box` refuses to build one), so a compose file is opened in the VM.
  One the box made a FIFO or a link to a device would hold the client there for ever, so the guest
  runner refuses a `-f` file that isn't a regular file and says why (`runner.py`'s
  `_refuse_special`), and its `services` read takes one as unreadable. That doesn't close the class:
  the check is a `stat` the box can race (compose re-opens the file after it), and a file an
  `include:` or `extends: file:` names isn't checked at all (the runner parses no YAML for it). So
  an interactive verb (`fy up`, `fy build`, `fy ps`, `fy logs`, `fy down`) can still hang on one
  until you press Ctrl-C. What runs without you is bounded instead: every compose call the posture
  reconcile makes runs under one deadline (`stack.RECONCILE_TIMEOUT`), so a client that never
  returns can't hang `fy mode` or the TUI. Running out is a reconcile skipped, never one done. The
  supervisor's tick never runs it, nor reads a compose file: a TTL expiry writes the mode and
  records the lapse (`devmode.record_stack_lapse`), and `fy doctor` asks the stack scope's drift
  verdict whether the stack has caught up, every call behind it under one deadline
  (`reconcile.VERDICT_TIMEOUT`).
- **Names from config in the TUI**: never in a widget id or a selector. A switch name only has to be
  printable, and Textual raises on an id that isn't an identifier (`api.v2`, `a[b]`). A readable one
  can also be foldyard's own (switch `fix` made `fix-on`, a Doctor repair button's id). Keep the
  name as data on the widget, give it an id of its own (`ModeButton`'s digest), and find the widget
  by its data (`AxisRow.button`).
- **Git on main or a registered checkout**: `.git/config` and the hooks are the box's, and git reads
  them. Git that can check out, update a ref or refresh the index goes through `worktree._host_git`:
  no hooks, no fsmonitor, no fetch, and no run at all when the config can't be scanned for hooks.
  Git that only reads goes through `worktree._git_read`. A worktree's `.git` file is the box's too,
  even for one your computer registered, so git doesn't run in a linked worktree for what main
  knows. Its branch is read from main's `.git/worktrees` (`worktree._branch_read`). Its bound config
  carries main (`Config.main`, set by `devmode.worktree_config`): code that needs "this checkout's
  main" takes it from there (`devmode.main_repo`, the `[ports]` pools).
- **Which project a directory belongs to**: your computer's records decide, and what the box can
  write may only make fy refuse, never steer it. The records (`config._records`) are every adopted
  main (`configpin.adopted_worktrees_roots`), its raw registry entries
  (`worktree_registry.recorded`) and its worktrees root from its ADOPTED config, counted only while
  it is dedicated (not main, not a directory holding main) and, inside main, reached one directory
  at a time through no link (`config._walked_ident`). Which records stand is decided once per read
  by what their real paths ARE (device and inode, `config._Claim`), never by how a path is spelled
  (case, Unicode normalisation): a record reached through a link when read stands for nothing, a
  main that another main REGISTERED as its worktree is that worktree, a root that overlaps another
  project's main, registered worktree or root (but for one inside another's root) claims nothing,
  and a main or registered worktree inside another's checkout or worktree is skipped
  (`config._judged`, `config.NESTED`: the two walls bridged). Adopting any overlap is refused
  (`config.nesting_conflict` in `configpin.adopt` and `fy init`, `config.claimed_elsewhere` in
  `fy worktree add`, twice), mounting a root that overlaps is refused (`config.mount_conflict` in
  `machine.ensure`/`recreate` and the stack's env, a doctor FAIL), and what doesn't stand is said in
  the doctor of each project it concerns alone. The CLI's front door pins its placement for the verb
  (`config.pin_placement`), so a plant mid-verb changes nothing it reads. Each lookup then counts a
  record only while its path is its real path (`config._live_ident`: a link swapped in above it
  would make it another directory), so nothing a box does outlives its undoing in the cache.
  `config.placement` walks up from the real path, matching each record by device and inode
  (`config._ident`, case folds), and the nearest wins; the checkout is the record's own path, which
  its adoption is keyed by. Between there and the record a `.git` (file or directory) or a
  `foldyard.toml` refuses (a submodule's `.git`, naming a git dir under `modules/` in main's git
  dir, is the checkout's own), a registered worktree's `.git` must point into its main's git dir's
  `worktrees` (by identity: `--separate-git-dir` mains too), the root itself, a directory several
  mains claim and a stray named like a registered worktree (case folded) refuse, and under no record
  a linked worktree's `.git` refuses, saying so for a bare repository's. `config.placement_here`
  adds the path you came by (`FOLDYARD_REPO` as written, or the shell's `$PWD`): a logical path the
  records place elsewhere refuses. Git isn't asked, and a `.git`'s word is at most a path or the
  words of a refusal (`Placed.main` is the records'). Every resolver goes through it (the CLI's
  callback, `config`'s ambient readers, `stack.find_main_repo` and `main_checkout_of`,
  `devmode._main_checkout`, `configpin.adopted_checkouts` for `fy ls`), and a new one must too. Help
  and the project-free verbs read no records and ask no git (`config.unplaced`). The one verb that
  runs where fy refuses is `fy box exec` in a worktree made outside fy inside a recorded main
  (`Placed.within`): the callback points it at that main (`FOLDYARD_REPO`, `WORKTREE=""`), so it
  names nothing from the worktree.
- **Waiting on git**: git blocks for good on a FIFO it finds in `.git` (an include, a
  `.git/worktrees/<id>` record). These take a timeout, and read running out as "git can't say", said
  as that rather than as a config problem (`worktree.GitTimeout`): finding main
  (`stack.find_main_repo`, `stack.main_checkout_of`, on your computer), `stack.pin_filemode`,
  `worktree._worktree_listing`, the TUI cards' branches (`devmode._CARD_GIT_TIMEOUT`, which
  `_git_read`'s config scan keeps to), and `git worktree add`/`remove`/`prune`
  (`_host_git(timeout=)`). `fy worktree remove` asks for the listing before anything is torn down,
  and refuses when it runs out. A new git a UI or every verb waits on belongs in this list.
- **A checkout your computer didn't register**: nothing runs in it, git included (not even
  `rev-parse`), since its `.git` file can name a gitdir, and with it a config, that the box built.
  Read what you need the way `worktree._branch_read` does:
  - Build the path from parts the host knows (`worktree._own_admin_dir`).
  - Open it with `O_NOFOLLOW | O_NONBLOCK`, check `S_ISREG`, and cap the size.
  - Only return a value of the shape you expect.
  - Name it from main's adopted config (`config.worktree_identity`), not from the directory.
  - Find its main the way a registered one's is found (`Config.main`), never from git in it.
- **Engine listings**: read `ps --format json` and act only on the engine's own `Id`
  (`stack._project_services`). In a `--format` template with one line per container, a label holding
  a newline starts a line of its own, and its first word reads as an id.
- **Verdicts built on labels or the guest's answers** (the stack scope's drift, which `fy state`
  shows) are the box's to shape, so they may only inform. When that verdict says the stack is in
  line, the lapse row in `fy doctor` reads ✓ and your computer drops its record (in the box, the row
  just goes): acceptable only because the row is advisory, since the supervisor already stopped the
  level's token service.
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
4. Does code on your computer open a path, or run git, under something the box can write? A read
   goes through `mountwrite.read_regular`.
5. Does a decision on your computer read the tree's config instead of the adopted copy?
6. Does a consent prompt put box-chosen text beside the thing being consented to?
