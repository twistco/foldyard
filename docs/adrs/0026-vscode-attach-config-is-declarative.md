# ADR-0026 — The VS Code attached-container config is declarative `[vscode]` data, read from the adopted copy

- **Status:** Accepted (2026-09-14). Built: `[vscode] extensions` / `[vscode.settings]` read via
  `devmode.worktree_config()` behind `configpin.gate("fy code")`; `remoteUser` and the daemon-port
  pin as foldyard facts; the machine-settings marker reset. Deleted: the in-box generator path
  (`vscode._generate_attached_config`, its python shim, output caps and the `_ALLOWED_CONFIG_KEYS`
  sanitiser) and any read of `.vscode/extensions.json`.
- **Sources:** the one consumer's `vscode-attached-config.py` (Tangible's), read line by line; the
  question "can the box make host VS Code install an extension?", asked during that reading.
  Related: [0022](./0022-host-runs-the-adopted-config.md) (the channel this joins),
  [0023](./0023-no-host-executed-code-from-the-repo-mount.md) §2 (the triage answer this amends for
  the VS Code surface), [0024](./0024-declarative-consumer-axes-no-repo-path-plugins.md) (the same
  finding, for plugins: *data wearing a class*).

## Context

[ADR-0023](./0023-no-host-executed-code-from-the-repo-mount.md) moved the consumer's attached-config
generator off the host and into the box: the script ran in the yard, printed a JSON document, and
foldyard sanitised it against a key allowlist before doing the one host-side write. That closed
"the generator runs on the host" and was the right first move. It left ~150 lines of machinery in
`vscode.py` — a python-or-uv shim, a timeout, temp-file sinks with a combined byte cap, a
replacement-decoding read-back, the allowlist walk with named drops — plus a dozen tests, all
defending against a document produced by code foldyard doesn't control.

Reading the only generator that exists, the document it produces decomposes into four jobs:

| what the script computed | whose fact is it |
| --- | --- |
| `remoteUser = "root"` | **foldyard's** — `fy shell`/`fy claude` exec `--user 0`; a consumer choosing otherwise would be a bug, not a preference |
| `extensions` = the union of N dirs' `.vscode/extensions.json`, minus `remote-containers` | **a list** |
| machine-scoped `settings` (`github.gitAuthentication`, an agent's skip-permissions flag) | **a table** |
| merging editor policy into the repo's gitignored `.vscode/settings.json` and generating the multi-root `.code-workspace` | **not foldyard's concern** — the consumer's editor policy, in a foldyard hook only because that hook happened to run on every `fy code` |

Nothing in the first three needs to be *computed* by consumer code; the fourth shouldn't be in
foldyard's path regardless of who computes it. This is [ADR-0024](./0024-declarative-consumer-axes-no-repo-path-plugins.md)'s
finding again: the thing that looked like it needed code was data.

**The finding that set the boundary.** ADR-0023 closed *who runs* the generator and left open
*what the generator decides*. The `extensions` list is consumed by the Dev Containers extension on
the host, with no click. Workspace-kind extensions install in the box — inside the boundary. A
**UI-kind** extension (`extensionKind: ["ui"]`, any marketplace author's choice) installs **on the
host** and runs as the operator — and the isolated `--user-data-dir` deliberately shares
`~/.vscode/extensions` with the operator's default VS Code, so it is installed for their everyday
editor too. The list came from `.vscode/extensions.json` files in the mount, which the box can
write; before that, from a script in the mount, which the box can write. So: anything that can write
the checkout could have host VS Code install and run a marketplace extension of its choosing. That
is host code execution from the repo mount, laundered through the marketplace — the class ADR-0023
exists to close, reached by a door it didn't look at.

Two smaller defects only became visible on the same reading. The script's own `_generatedBy` marker
and ownership check were dead (foldyard had taken both over), and its comment that changed machine
settings "need the marker deleted in the box before the next attach" described a gap foldyard had
never closed: it reset the *extensions* marker, not the *settings* one, so a changed setting silently
never applied to a box that had been attached to before.

## Decision

**`fy code` authors the attached-container config itself, from the ADOPTED `[vscode]` table. No
consumer code runs to produce it, and nothing under the mount is read for it.**

- `[vscode] extensions = [...]` — the auto-install list, as marketplace ids, id-validated,
  de-duplicated, with the Dev Containers extension dropped. **Config, not `.vscode/extensions.json`:**
  the list decides what the host installs, so it lives where the adopt gate reviews it. The
  recommendations files keep their plain-VS-Code role (click-to-install), which is the human gate
  this list bypasses. The duplication is the cost, and it is small.
- `[vscode.settings]` — a table carried into the config's `settings` (both schemas, so newer and
  older Dev Containers versions alike install rather than recommend). Settings can't execute; the
  schema's lifecycle hooks are not a key. The daemon-port pin merges on top and wins.
- **Read from the adopted copy, behind the gate.** `fy code` calls `configpin.gate("fy code")`
  then reads the table through `devmode.worktree_config()` — the funnel every host-consequence verb
  uses ([ADR-0022](./0022-host-runs-the-adopted-config.md)). A box edit to `[vscode]` is inert
  until an operator adopts it, and the adoption diff is where "this branch wants `x.y` installed
  on your machine" is seen. `fy code` had read the tree ambiently before (for `[vscode]`'s presence
  and `workspace_file`); it no longer does for anything.
- `remoteUser` is pinned to `root` by foldyard, not declared.
- foldyard keeps the last config it wrote as the record of what the box's Machine settings hold,
  and clears `.writeMachineSettingsMarker` whenever the settings differ (a first write included) —
  the twin of the existing `.installExtensionsMarker` reset.
- The `_generatedBy` ownership contract is unchanged and all-or-nothing: remove the key and
  `fy code` leaves the file alone, so the table stops applying to that instance. It is a host-side
  file under `~/.foldyard`, so the box can't reach it; the per-operator lever for *tweaking* rather
  than opting out is `foldyard.local.toml`, which deep-merges and is adopted alongside.

The allowlist goes with the generator: it existed to bound a document foldyard didn't author. With
foldyard naming every key itself, a consumer-supplied `initializeCommand` has no path into the file.

## Considered and not done

**Shadowing `.vscode/` (and the `.code-workspace`) with overlay volumes so the box can't edit
them.** It would defend the residual channel — VS Code itself still reads the mount's `.vscode/`
when attached — but the boundary this codebase draws is *host consequences come from adopted
config*, not *mount files are immutable*, and the second is a losing game: `.vscode/`,
`*.code-workspace`, `.editorconfig`, a `pyproject` naming a tool path — every file an editor reads
is a candidate. It also breaks the yard's own model (agents legitimately edit workspace settings;
a shadow diverges from the checkout under branch switches, on both sides of the mount). The residual
exposure is VS Code's workspace-trust model for any trusted workspace, and in an attached window
nearly all of it executes in the box: the extension host, tasks, launch configs and terminals all
run remote. What executed on the host without a click was the `extensions` list, and that is what
moved. If a *specific* `.vscode` key is ever shown to reach the host, the fix is the same shape as
this one — name it, pin it, or move it to config — not a mount-wide overlay.

## Consequences

- The consumer's script shrinks to nothing, or to its fourth job. Tangible's editor-policy merge
  (per-folder formatters, the generated workspace file) now needs a home outside foldyard — commit
  the files, or keep a repo-side task; the reason they were gitignored (Peacock writing per-user
  state into the workspace file) is Tangible's to weigh. `[vscode] workspace_file` stays: a thin,
  honest key ("open this instead of the folder"), never the part that needed code.
- `remote.autoForwardPorts = false` — the request that surfaced all this — is one line of config.
- An agent adding an extension on a branch now needs an operator to adopt before it installs.
  That friction is the point: it is the exact moment the operator should see the id.
- `fy config widenings` does not yet list `[vscode] extensions`. It should — it is a repo-declared
  ask of the host in the same sense as an injection target — and is a small follow-up, not a
  boundary: the gate is what stops the box, the widenings row is what tells the operator.
- A future surface that genuinely needs *computed* output from the yard should reach for ADR-0023
  §2's split again — this ADR removes one instance of it, not the pattern.
