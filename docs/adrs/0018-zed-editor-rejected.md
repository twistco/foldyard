# ADR-0018 — Zed editor: investigated, rejected for now

- **Status:** Rejected (for now, 2026-06) — design shovel-ready if revisited
- **Sources:** docs/history/agent-editor-plugins-plan.md Item 4a

## Context

The editor plugins needed a second editor beside VS Code, and Zed was the obvious candidate. The
question was the **remote transport**: how does a Mac-side editor reach a dev box that, by
design, holds no `~/.ssh` and accepts no inbound credential?

VS Code answers it without SSH: the Remote-Containers extension talks to the **container engine
API** over the mounted socket (`DOCKER_HOST`), `exec`s into the running box, runs the VS Code
Server there, and streams over the exec stdio — no sshd, no network port, no inbound credential.
That is exactly why `fy code` is a Mac-side verb that only needs the socket env, and why VS Code
rides the same rootless-socket trust boundary as everything else.

Zed was investigated against its official docs and source. Zed remote development is **SSH-only**:
the local Zed shells out to the system `ssh` binary, establishes a ControlMaster, and uploads and
runs a `zed-remote-server` binary in `~/.zed_server` on the remote (`upload_binary_over_ssh:
true` for restricted-internet remotes — which the box is). There is no container-engine-exec
transport.

## Decision

**Skip Zed for now.** Supporting it means running sshd inside the box — host keys,
`authorized_keys`, an inbound auth surface — on a box whose design deliberately keeps *no*
`~/.ssh` and *no* inbound credential. That inversion of the box's credential posture is not worth
a second editor while VS Code covers the need over the engine socket.

The least-bad design was worked out and recorded as shovel-ready, should demand return: `sshd -i`
(inetd mode, one session on stdin/stdout, no listener, no published port) behind an
`~/.ssh/config` `ProxyCommand podman exec -i <box> …` — Zed honours ssh_config since it shells
out to `ssh`. That reuses the same engine-socket trust boundary as VS Code, mounts only the Mac's
*public* key, and puts nothing on the VM's network. It is strictly better than publishing port
22, but still adds the sshd surface and stayed unbuilt.

## Consequences

- VS Code is the supported attach editor (server volume + engine-exec attach, `fy code`); the
  `vscode` plugin ships in core, inert until `[vscode]` is declared (ADR-0015).
- No `[zed]` plugin, no sshd, no host keys in the box image or bootstrap — the no-inbound-creds
  property stays absolute rather than "except for the editor".
- **What would change the call:** Zed shipping a non-SSH remote transport (a container/engine-exec
  mode, or any stdio transport foldyard can supply without sshd). Short of that, a recurring user
  need would revive the `ProxyCommand` + `sshd -i` design above, gated behind an opt-in `[zed]`
  table with the key material kept to the public half.
