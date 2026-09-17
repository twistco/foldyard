# ADR-0026 — The VS Code attached-container config is declarative `[vscode]` data, read from the adopted copy

- **Status:** Accepted (2026-09-14). Built: `[vscode] extensions` / `[vscode.settings]` read via
  `devmode.worktree_config()` behind `configpin.gate("fy code")`; `remoteUser` and the daemon-port
  pin as foldyard facts; the machine-settings marker reset; one attach shape (the checkout folder).
  Deleted: the in-box generator path and its sanitiser, any read of `.vscode/extensions.json`, and
  `[vscode] workspace_file`.
- **Sources:** the one consumer generator script that existed, and the question "can the box make
  host VS Code install an extension?", asked while reading it. Related:
  [0022](./0022-host-runs-the-adopted-config.md) (the channel this joins),
  [0023](./0023-no-host-executed-code-from-the-repo-mount.md) §2 (the triage answer this amends for
  the VS Code surface), [0024](./0024-declarative-consumer-axes-no-repo-path-plugins.md) (the same
  finding, for plugins: *data wearing a class*).

## Context

`fy code` attaches an isolated VS Code instance (its own `--user-data-dir`, one per worktree) to the
running dev box. "Attach to Running Container" ignores `devcontainer.json`; the lever for installing
extensions and applying settings on attach is an *attached-container config* that VS Code reads from
the instance's own globalStorage. Under [ADR-0023](./0023-no-host-executed-code-from-the-repo-mount.md)
a consumer script produced that document in the box and foldyard sanitised it against a key
allowlist before the one host-side write.

Everything such a script can compute decomposes into three kinds of thing:

| what it produced | whose fact it is |
| --- | --- |
| `remoteUser` | **foldyard's** — `fy shell`/`fy claude` exec as root; a consumer choosing otherwise would be a bug |
| `extensions` | **a list** |
| `settings` | **a table** |

Nothing there needs to be *computed*. This is [ADR-0024](./0024-declarative-consumer-axes-no-repo-path-plugins.md)'s
finding again: the thing that looked like it needed code was data.

**The finding that set the boundary.** The `extensions` list is consumed by the Dev Containers
extension on the host, with no click. Workspace-kind extensions install in the box — inside the
boundary. A **UI-kind** extension (`extensionKind: ["ui"]`, any marketplace author's choice)
installs **on the host** and runs as the operator — and the isolated `--user-data-dir` deliberately
shares `~/.vscode/extensions` with the operator's default VS Code, so it is installed for their
everyday editor too. Sourced from a script or from `.vscode/extensions.json` files in the mount,
that list let anything that can write the checkout have host VS Code install and run a marketplace
extension of its choosing: host code execution from the repo mount, laundered through the
marketplace — the class ADR-0023 exists to close, reached by a door it didn't look at.

## Decision

**`fy code` authors the attached-container config itself, from the ADOPTED `[vscode]` table. No
consumer code runs to produce it, nothing under the mount is read for it, and the attach is always
the checkout folder.**

- `[vscode] extensions = [...]` — the auto-install list, as marketplace ids, id-validated,
  de-duplicated, with the Dev Containers extension dropped. **Config, not `.vscode/extensions.json`:**
  the list decides what the host installs, so it lives where the adopt gate reviews it. The
  recommendations files keep their plain-VS-Code role (click-to-install), which is the human gate
  this list bypasses. The duplication is the cost, and it is small.
- `[vscode.settings]` — a table carried into the config's `settings` (both schemas, so newer and
  older Dev Containers versions alike apply it). Settings can't execute; the schema's lifecycle
  hooks are not a key. The daemon-port pin merges on top and wins.
- **Read from the adopted copy, behind the gate.** `fy code` calls `configpin.gate("fy code")`
  then reads the table through `devmode.worktree_config()` — the funnel every host-consequence verb
  uses ([ADR-0022](./0022-host-runs-the-adopted-config.md)). A box edit to `[vscode]` is inert
  until an operator adopts it, and the adoption diff is where "this branch wants `x.y` installed
  on your machine" is seen.
- `remoteUser` is pinned to `root` by foldyard, not declared.
- foldyard keeps the last config it wrote as the record of what the box's Machine settings hold,
  and clears `.writeMachineSettingsMarker` whenever the settings differ (a first write included) —
  the twin of the `.installExtensionsMarker` reset. Dev Containers applies an attached config only
  once per server install; without this, a changed setting never reached a box that had been
  attached to before.
- The `_generatedBy` ownership contract is all-or-nothing: remove the key and `fy code` leaves the
  file alone, so the table stops applying to that instance. It is a host-side file under
  `~/.foldyard`, so the box can't reach it.
- **One attach shape.** `fy code` opens the checkout folder (`--folder-uri`), never a workspace
  file. A worktree is attached the same way for its whole life, so window-scoped state VS Code saves
  in one session is read by the next.

The allowlist goes with the generator: it existed to bound a document foldyard didn't author. With
foldyard naming every key itself, a consumer-supplied `initializeCommand` has no path into the file.

### Where settings land, and what that is for

VS Code resolves settings in an attached window in this order, later wins:

```
User  (the isolated instance's own settings.json, under ~/.foldyard)
  <  Remote [Machine]  (the box's vscode-server — where [vscode.settings] is applied)
    <  Workspace  (the checkout's .vscode/settings.json, in the mount)
```

Each layer has one job:

- **Remote [Machine] is the team's policy.** `[vscode.settings]` is the only thing foldyard puts
  there: editor defaults, per-language formatters, tool paths, agent flags. It sits on the persisted
  vscode-server volume, so it survives box recreation, and it is the slot VS Code designed for
  "settings for this environment" — **machine-scoped** keys (interpreter and binary paths) are
  honoured here and *ignored* at the local user level, which is why this and not the instance's
  user settings is the delivery target.
- **Workspace is the person's.** The checkout's `.vscode/settings.json` overrides the policy, and
  foldyard never writes it, so a consumer can keep it gitignored and let each operator hold their
  own tweaks there — window colours per worktree, a personal ruler — without a diff. Being
  per-checkout, it is per-worktree by construction.
- **User is foldyard's instance plumbing** (the local-terminal shims, the port pin, `update.mode`)
  and nothing a consumer declares.

Per-operator overrides of the *policy* go in `foldyard.local.toml`: its `[vscode.settings]`
deep-merges key-by-key over the shared table and is adopted alongside it, so one setting can be
changed without redeclaring the team's. Overrides of the *experience* go in the workspace layer.

## Considered and not done

**Delivering `[vscode.settings]` into the instance's User settings instead.** foldyard already owns
that file, and the write would be immediate rather than marker-gated. Rejected because User is the
lowest layer and the wrong scope for a remote window: machine-scoped keys are silently dropped
there, so the first path-shaped setting a consumer added would not apply. Remote [Machine] is the
designed slot; the marker reset makes it reliable.

**A `workspace_file` key (open a multi-root `.code-workspace` instead of the folder).** It carried
one consumer's per-folder-formatter workaround and gave one worktree two attach shapes over its
life: a first attach on the folder (the generated file did not exist yet), later ones on the file.
VS Code reads window-scoped settings from the folder's `.vscode/settings.json` in a single-root
window but only from the workspace file in a multi-root one, so what an operator set in the first
session silently stopped applying in the second. Per-folder formatter defaults are a consumer's
editor policy, expressible by committed per-folder `.vscode/settings.json` files, and not
foldyard's concern.

**Shadowing `.vscode/` with overlay volumes so the box can't edit it.** It would defend the residual
channel — VS Code itself still reads the mount's `.vscode/` when attached — but the boundary this
codebase draws is *host consequences come from adopted config*, not *mount files are immutable*,
and the second is a losing game: `.vscode/`, `.editorconfig`, a `pyproject` naming a tool path —
every file an editor reads is a candidate. It also breaks the yard's own model (agents legitimately
edit workspace settings; a shadow diverges from the checkout under branch switches, on both sides
of the mount). The residual exposure is VS Code's workspace-trust model for any trusted workspace,
and in an attached window nearly all of it executes in the box: the extension host, tasks, launch
configs and terminals all run remote. What executed on the host without a click was the
`extensions` list, and that is what moved. If a *specific* `.vscode` key is ever shown to reach the
host, the fix is the same shape as this one — name it, pin it, or move it to config — not a
mount-wide overlay.

## Consequences

- A consumer generator script has no job left. Editor policy it merged into the checkout moves to
  `[vscode.settings]`; extensions it collected move to `[vscode] extensions`; a multi-root
  workspace it generated is replaced by committed per-folder settings.
- **`[vscode.settings]` reaches the `fy code` window only.** A native VS Code window on the same
  checkout sees neither the policy nor, if the consumer gitignores it, a workspace file. A team
  that needs policy in native windows commits `.vscode/settings.json` instead and gives up the
  personal layer there — that trade-off is the consumer's, and this ADR only makes it explicit.
- An agent adding an extension on a branch now needs an operator to adopt before it installs.
  That friction is the point: it is the exact moment the operator should see the id.
- `fy config widenings` lists `[vscode]` as the editor attach — not for the extensions (a
  follow-up: they are a repo-declared ask of the host in the same sense as an injection target)
  but for what the attach itself does. Found while this ADR was in review (2026-09-17): Dev
  Containers forwards the host's SSH agent and git-credential store into every terminal it opens
  in the box, and no setting stops it (launching VS Code with `SSH_AUTH_SOCK` stripped still
  forwards — macOS shell-env resolution). A live agent with one key sat in a box whose posture
  read "never push"; the defeat one consumer's image carried (an rc-file unset) was never
  foldyard's, and the socket is usable by path regardless of the env. So foldyard's bootstrap now
  installs the hygiene (`box._HARDEN_SNIPPET`: unset + a socket reaper), `fy code` re-ensures it
  before the attach, the egress wall fences CONNECT to `:443`, and `fy verify` reports the vars
  AND the sockets. The gate is still what stops the box; this is what stops the attach.
- A future surface that genuinely needs *computed* output from the yard should reach for ADR-0023
  §2's split again — this ADR removes one instance of it, not the pattern.
