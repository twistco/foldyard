# Agents in sessions: `fy agent`

`fy claude` and `fy codex` run the agent in the terminal you start them from: close that terminal
and the agent stops. `fy agent` runs the same agent in a **tmux session in the box** instead, so
it keeps working with no terminal attached, and any terminal on your computer can reattach to it
later. `fy agent ls` lists every agent across your local projects.

It's a separate command on purpose: under tmux, scrolling, selecting text and a few keys behave
differently ([below](#what-changes-under-tmux)). `fy claude` stays exactly as it was.

## Start, list, attach, stop

```bash
fy agent start                       # Claude, in this checkout's box (from your computer or the box)
fy agent start codex --name review   # Codex, with a name of your choosing
fy agent start claude -a -- --model opus   # attach straight away; args after -- go to the agent
fy agent ls                          # every local project's agents
fy agent attach 3f2c9a1e             # reattach, from anywhere on your computer
fy agent resume 3f2c9a1e             # pick an exited agent's conversation up again
fy agent stop 3f2c9a1e               # stop the agent and remove its session
fy agent clean --older-than 7d       # remove sessions that exited over a week ago
```

`fy agent start` prints the session's **id**. For Claude it's Claude's own session id: foldyard
passes it as `--session-id`, so it's the id the transcript is named after, and the one a
transcript browser shows you. `--resume <id>` keeps that id. The exception is when Claude picks
the session itself, with `--continue`, a bare `--resume` or `--fork-session`: the id is then
foldyard's own, and the transcript's id won't attach to it (`fy agent ls` shows foldyard's).
`fy agent attach` and `fy agent stop` take the id, its first 8 or more characters, or the
session's name.

Detach with **Ctrl-B d**. The agent keeps running.

**From inside a box**, `fy agent attach` and `fy agent stop` reach that box's own sessions: in a
`fy box shell` the attach takes over the shell's terminal, and from inside another agent's session
it switches that terminal over to the one you named (Ctrl-B d still detaches). A session in
another worktree's box is attached from your computer.

**Where `fy agent ls` looks.** On your computer it lists every project you've adopted a config for
(with `fy config adopt`, or when a launch command such as `fy up` asked you to), and in each one the boxes of the main
checkout and its registered worktrees. A project whose VM isn't running is listed under *Not
listed* with the reason. foldyard never starts a VM to answer. `--here` lists only the project
you're in, and `--json` prints the listing as JSON. Inside a box, `fy agent ls` lists that box's
own sessions.

**Stopping a box stops its agents.** `fy box down` names the agents still running in the box
before removing it, and asks first when run in a terminal (`--yes` skips the question). The TUI's
stop-box confirmation lists them too. The same holds for anything that recreates the box or stops
the VM: the sessions go with it.

**Resuming a conversation.** `fy agent resume <id>` picks an agent's conversation up again, in a
new session in this checkout's box: Claude's by `--resume`, Codex's by `codex resume`. If the box
still holds the old session and it has exited, resume removes it first, and the new session keeps
its name and id; the id's first 8 characters or the name find it. If it's still running, resume
says so and leaves it alone: attach instead. Arguments after `--` go to the agent, as with
`start`.

- **Claude** resumes by its own session id, which is the session's id (unless Claude picked the
  session itself, with `--continue`, a bare `--resume` or `--fork-session`).
- **Codex** takes no session id from outside, so foldyard records Codex's own thread id on the
  session once Codex has one. Codex saves a thread at your first message, so a session you never
  typed in has nothing to resume. `fy agent ls --json` shows it as `conversation`, and its full id
  finds the session too.

With no session left (after the box was recreated, say), give the agent's own full id: from
`fy agent ls --json` beforehand, the transcript, or what Codex prints when it quits. Add
`--agent codex` for a Codex one (Claude is the default), which also takes a name you gave the
thread with Codex's `/rename`.

**An agent that exits leaves its session behind.** `fy agent ls` shows it as `exited`, and
attaching to it shows its last screen, which is usually the reason it stopped. `fy agent stop`
removes one. `fy agent clean --older-than <age>` removes every session that exited longer ago than
`<age>` (`30m`, `12h`, `7d`, `2w`; 1 day by default), across your local projects or, with
`--here`, this one. `--dry-run` lists them first. A running agent is never removed, whatever its
age.

## What changes under tmux

- **Ctrl-B is tmux's key.** Claude Code uses Ctrl-B to send a running command to the background;
  in a session, press **Ctrl-B Ctrl-B**.
- **Scrollback is tmux's, not your terminal's.** Scroll with **Ctrl-B [** (then the arrow keys or
  Page Up, `q` to leave). Your terminal's own scrollback only holds what was on screen.
- **Shift-Enter** reaches the agent as its own key, so Claude's multi-line input works. foldyard
  turns tmux's extended keys on for this, and your terminal has to send them too.
- **A second attach doesn't shrink the first.** The window follows whichever terminal used it
  last.
- **Pasting an image doesn't work in the box**, with or without tmux: Claude reads a pasted image
  from the clipboard, and the box doesn't have yours. A file works: save the image inside the
  checkout (the box sees it at the same path; a gitignored folder keeps it out of commits) and
  give Claude its path.

## Two settings that cut down what reaches your terminal

foldyard runs its own tmux server in each box (`tmux -L fy`, never the one you might start
yourself) and sets its options every time it starts a session, instead of reading a config file.
Two of those options limit what an agent's output can do to your terminal:

- **`allow-passthrough off`**. tmux redraws the screen itself, so an agent's raw escape sequences
  don't reach your terminal. Without tmux, everything the box prints goes to your terminal as it
  is.
- **`set-clipboard external`**. tmux ignores a program's request to set your clipboard.

**These are defaults, not a boundary.** The tmux server and the `tmux` program both run in the
box, so anything in the box can turn the settings back on. Code that means to attack your terminal
can reach it when you attach, just as it can through `fy box shell`. What the settings stop is an
agent's ordinary output doing it: a tool that prints an escape sequence, or a README with a
clipboard-setting sequence in it, passed through to your terminal while you watch. Use a terminal
that asks before letting a program set the clipboard.

tmux older than 3.3 passes escape sequences through whatever the first setting says, so
`fy agent start` refuses it. The rest of the settings are papercuts: no delay after Esc (which
interrupts Claude), Shift-Enter, the window size, focus events (so Claude knows when its pane
gains or loses focus) and a 50,000-line scrollback. An attach also tells tmux that your terminal
draws UTF-8 (without it, every non-ASCII character showed as `_`) and, when your terminal sets
`COLORTERM=truecolor`, that it shows 24-bit colour. Claude draws in 24-bit colour under tmux, and
tmux picks the nearest colours for a terminal that can't show them.

## Your own box image

foldyard's packaged box image includes tmux. If you set `[box].image`, add it to your image or
let foldyard install it:

```toml
[box]
packages = ["tmux"]
```

Then `fy box down && fy box up` (adopt the config when asked). When tmux is missing,
`fy agent start` offers to add that line for you: it asks first, edits only `[box].packages`
(in `foldyard.local.toml` if that file sets the list, else `foldyard.toml`), keeps the file's
comments and layout, and refuses an edit it can't make exactly. On foldyard's packaged image
tmux is already there, so a box without it was built from an older image: `fy agent start`
says to recreate it instead.

## Reading transcripts in a browser

`fy agent` has no web interface of its own. To read what your agents are doing in a browser, use a
transcript viewer such as [claude-code-log](https://github.com/daaain/claude-code-log) over the
archive the supervisor keeps on your computer. First have the supervisor keep that archive
current:

```toml
[claude]
transcript_sync_seconds = 10   # copy the box's transcripts to ~/.claude/projects every 10 s
```

Then `fy config adopt`: your computer runs the config you adopted, not the file as edited. Then
turn the archive into pages and open them as files:

```bash
claude-code-log --open-browser   # writes HTML beside the transcripts, opens index.html
claude-code-log watch            # keeps the pages current as transcripts arrive
```

**Use the files, not `claude-code-log serve`.** A server on your computer's loopback is reachable
from the box unless the VM firewall is on (`[machine] firewall = true`): the VM's network hands
its gateway to your computer's loopback, and a `Host` check doesn't stop a box that sends the
right one. `serve` would show it the whole archive, including your own Claude sessions from every
other project. Files opened in your browser have no listener for the box to reach.

Point the viewer at the archive (`~/.claude/projects`, its default), never at the box's
transcripts directly. The box writes those, and a link it plants there could point a viewer at
any file on your computer; the archive is copied without following links. For a session started
with `fy agent start`, the id it shows is the one `fy agent attach` takes, except for a session
Claude picked itself (see [the exception above](#start-list-attach-stop)). The archive also holds
sessions started some other way, such as a plain `fy claude`; those have no tmux session to attach
to.

## Why it's built this way

- **No listener.** Your computer reaches each box over the VM's container socket (`podman exec`),
  as every other `fy` command does.
- **Everything a box reports comes from the box.** Session names, states and times are read from a
  tmux server the box runs, and a box can reach its sibling worktrees' boxes. foldyard shows what
  they say, cut to printable characters, and acts on them only to attach to or stop the session
  you named.

See [security](./security.md) for what the box can and can't reach on your computer.
