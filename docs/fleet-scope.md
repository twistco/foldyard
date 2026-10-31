# Agent fleet: scope

Status: **v1 built (`fy agent`), next phase proposed**, 2026-10-31. The user-facing page is
[agents.md](./agents.md). This note records what v1 decided and left out, and scopes the next
phase: a control plane hosted away from your computer, which can type into agents as well as watch
them. Related: [ADR-0033](./adrs/0033-a-to-box-and-a-from-box-beside-the-checkout.md) (the
from-box, where transcripts come out), [security.md](./security.md) (what the box can reach).

## What "fleet management" breaks down into

Four capabilities, each with a different direction of trust:

| capability | what | direction | v1 |
| --- | --- | --- | --- |
| persistence | an agent outlives the terminal that started it | (box-internal) | ✓ tmux in the box |
| observation | what each agent is doing, which ones need you | box → you | ✓ by transcripts, in a viewer |
| aggregation | one view across worktrees and projects | host-side | ✓ `fy agent ls` |
| interaction | typing into an agent, sending it images, from elsewhere | you → box | attach only |

## v1, as built

- **A separate verb, not a change to `fy claude`.** Under tmux, scrollback, selection, Ctrl-B
  (Claude backgrounds a command with it) and Shift-Enter all behave differently. That's fine for
  someone who chose sessions, and a regression for someone who didn't.
- **tmux in the box, on foldyard's own server.** The server is `tmux -L fy -f /dev/null`. Its
  options are passed as argv on every start (`agents._SERVER_OPTIONS`), so no file in the box
  configures them. `allow-passthrough off` and `set-clipboard external` are posture: tmux redraws
  the screen, so an agent's raw escape sequences don't reach your terminal, and it can't set your
  clipboard. Today `fy box shell` passes both through unfiltered. tmux < 3.3 is refused because
  it always passes escape sequences through. The packaged image installs tmux. It isn't added to
  the image contract (ADR-0014): a consumer's own image adds `packages = ["tmux"]`.
- **One id per session.** foldyard makes a UUID and gives it to Claude as `--session-id`, so the
  transcript's id, the id a viewer shows and the `fy agent attach` handle are the same string.
  `--resume <uuid>` keeps that uuid. Codex has no such flag, so its id is foldyard's own.
- **Observation is the transcripts, read by a viewer.** foldyard serves no page.
  [claude-code-log](https://github.com/daaain/claude-code-log) escapes rendered HTML
  (`escape=True`, Jinja autoescape), binds to loopback and checks `Host`. Pointed at the archive
  the supervisor's no-follow sync writes (`transcript_sync_seconds`), it is the browser view. It
  must never read the from-box directly: both its conversion and its file server follow links.
- **No listener anywhere.** The host reaches boxes over `podman exec` on the VM's socket, as
  every other host verb does.
- **Aggregation by the host's own record.** The project list is the adopted main checkouts
  (`configpin.adopted_checkouts`, read from host-only markers). Each is asked by running
  `fy agent ls --here --json` in its own checkout, so each project resolves its own VM, prefix and
  worktrees through the same code as a command run there. A stopped VM is reported, never
  booted.
- **Box-written data stays data.** Session names, ids and times come from a tmux server the box
  controls (and any box can reach its siblings' servers). They are cut to printable characters and
  validated (ids must be UUIDs), and they are acted on only to attach to or stop the session the
  operator named.
- **Teardown names what it kills.** `fy box down` lists live sessions and asks on a terminal, and
  the TUI's stop-box confirmation lists them. `fy worktree remove`, which has its own
  confirmation, passes `--yes`.

### Left out of v1, knowingly

- **Agent state in `fy agent ls`** (waiting for you, working, idle). The viewer shows it from the
  transcript. `ls` shows only tmux liveness: running, attached, exited. A transcript can't tell a
  dead agent from an idle one, so a later "needs you" signal must join the two: the transcript for
  the meaning, tmux for whether anything is alive.
- **Sessions outliving the box.** They die with a box recreate or VM stop.
  `fy agent start claude -- --resume <id>` picks a Claude session up again.
- **Images.** Claude reads an image from the clipboard or a path, and the box has neither yours.
- **Codex's transcript ↔ session link.**
- **Freshness.** The viewer is as current as `transcript_sync_seconds`.
- **Recreate paths other than `box down`** (`fy box up` recreating on drift, `fy machine stop`)
  don't name sessions yet.
- **No host-tier e2e module.** v1 is golden-tested. A real box run (start → attach → detach → ls →
  stop) is a manual check for now.

## Next phase: a hosted control plane

The direction: the control plane runs in a small cloud VM, foldyard instances run in VMs sized to
each project, and one browser session (password, long-lived) watches and drives all of them.

### One control plane, two placements: local first

If the control plane is a reader rather than a daemon, so that it pulls state when someone looks
and caches what it pulled, it doesn't care where it runs. It can run on your computer beside the
hosts, or in a cloud VM in front of them. So the local one is the first increment, not a separate
product:

- **Local:** run on demand (`fy agent …` today; a page later, served only while you have it open).
  The transport is the one v1 has, `podman exec` per box, and there is nothing to keep running.
  Its one new exposure is the page's listener, which the box can reach on loopback until the
  loopback fix lands. That is why the page waits for that fix, and why it needs the password from
  the start.
- **Cloud:** the same reader and the same page, with a different transport. A cloud VM can't
  reach a host behind NAT, so each host keeps an outbound connection open to it (the relay below).
  That relay is the one long-running piece, and only the cloud placement needs it.
- **What keeps them one design:** the reader asks the transport for a fixed verb set (list,
  attach, send keys, send a file, stop). Locally that set is `podman exec` calls. In the cloud
  it is the relay's protocol. Build the verbs as an interface in the local version, and the cloud
  version only adds a transport.

Caching is where "no daemon" gets tested. A cache is only as fresh as the last pull, so a "needs
you" signal needs someone polling. That means the page while it's open, or the supervisor, which
already runs per project and already notifies.

### Topology: the question to settle first

Today **the host is your computer**. It holds the credentials, runs the supervisor and the proxy,
and is the operator. "foldyard instances in custom-sized VMs" can mean two different things:

1. **foldyard hosts in the cloud.** A cloud VM plays the role your computer plays now (Lima inside
   it, or nested virtualisation), holding that project's credentials. The control plane then talks
   to many hosts, none of them your computer.
2. **Your computer stays the host, and project VMs are placed remotely.** That's a new machine
   backend (the VM elsewhere, its socket forwarded). The control plane talks to your computer.

These have different threat models: where credentials live, who runs `fy mode`, what a lost
laptop or a compromised cloud account takes with it. Everything below assumes the control plane
and the hosts are separate machines either way.

### Transport: hosts dial out, the control plane never dials in

- Each host runs a small **relay** with an outbound connection to the control plane. It
  authenticates with a per-host key pair, which is revocable. No host opens a listener.
- The relay offers a **fixed verb set**: list sessions, attach (a byte stream), send keys, send a
  file, stop. The control plane can't ask for anything else, because the relay has nothing else
  to run. Posture (`fy mode`, `fy allow`, `fy config adopt`) is **not** in the set: a compromised
  control plane must not be able to switch a credential on.
- The relay **logs every action host-side** (verb, session, size, time). That gives the operator
  an audit record the control plane can't edit.

### What the control plane would hold

A control plane that can type into agents is, in effect, code execution in every box it reaches.
Those agents run with permission prompts skipped, and with whatever switches are on. It also
receives transcripts, which are your code and your conversations. So:

- **Posture stays local to each host's operator**, as above.
- **Data leaving the host is opt-in per project.** Transcripts stop being "on your computer only".
- **Per-host consent.** A host joins the fleet by its operator running a command, never by the
  control plane asking.

### The web interface

- **Password with a long session**, as discussed:
  - `scrypt` hash (stdlib), rate-limited logins;
  - cookie `HttpOnly`, `Secure`, `SameSite=Strict`, plus an `Origin` check;
  - "sign out everywhere" by rotating the signing secret;
  - TLS is mandatory once it's off loopback.

  Passkeys are a later step.
- **Rendering box-written content.** Transcripts go through claude-code-log's escaping, and the
  page adds a strict CSP: once the same session can type into agents, escaping can't be the only
  layer. Terminal output in the browser (xterm.js or similar) comes with clipboard writes (OSC 52)
  off and links (OSC 8) shown, not followed.
- **No posture controls in the browser**, even as a convenience.

### Interaction

- **Typing:** `tmux send-keys -l` for literal text, then `Enter` on its own. For multi-line,
  `load-buffer -` then `paste-buffer -p`, which sends a bracketed paste, so the agent gets one
  message rather than one per line.
- **Images:** the relay streams the file into the box (`<engine> exec -i <box> sh -c 'cat >
  /tmp/fy-paste/<uuid>.<ext>'`), checking its size and type first, then types the path. This
  works on both backends and keeps the file private to that box. The to-box would also work
  (read-only to the box, host-written), but it is Lima-only and every box of the project can read
  it.
- **"Needs you":** transcript state joined with tmux liveness. The supervisor already sends
  notifications, so a local version of this may come before the hosted one.

### The claude-code-log side

Its transcript page already has a "resume session" button that copies a command. With the ids
aligned, a foldyard-aware mode could copy `fy agent attach <id>` instead.

## Open questions

- **Topology 1 or 2** (above). Everything else depends on it.
- **Who else views the fleet?** A second person turns the password into identity, the log into
  per-user records and the verb set into permissions.
- **Sizing:** does a project's VM size live in `[machine]` (adopted, so reviewed) or in the
  control plane, which would make the control plane a config source the host has to adopt like
  any other?
- **Is the loopback fix a prerequisite** for a *local* web view, or does a local view wait for the
  hosted one?
