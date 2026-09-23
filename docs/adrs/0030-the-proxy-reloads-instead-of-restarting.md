# ADR-0030 — The proxy reloads its settings instead of restarting

- **Status:** Accepted (2026-09-23). Implemented on `proxy-hot-reload`.
- **Sources:** the 2026-09-23 `repower` build failure below; `tests/test_proxy_reload_e2e.py`
  (a real mitmdump). Related: [0007](./0007-credential-injection-at-egress-proxy.md) (the proxy
  is where credentials are attached), [0022](./0022-host-runs-the-adopted-config.md) (the host
  runs the adopted config; reporting is part of the fix),
  [0029](./0029-the-proxy-always-decrypts.md) (decryption and `passthrough`).

## Context

The supervisor restarts a daemon whose command or environment changed. The proxy's environment
carried everything that varies with posture: the injection rules, the wall switch, the
passthrough list, and a hash of the secrets its rules read (because a minter read its secret
from an environment fixed at launch). So every mode switch, every learn window ending, every
adopted `passthrough` edit and every rotated or newly captured token restarted mitmdump. A
restart closes every connection it carries.

On 2026-09-23 a `fy box up` in `repower` was installing Playwright's system packages when the
Sanity axis was switched from Viewer to Editor. The proxy restarted between two packages; the
download in flight was cut, and the new process took a quarter of a second to listen. apt got
one "connection refused", treated the proxy as down for the rest of its run, and failed all
~40 remaining packages without trying them. Anything without its own retry fails the same way,
including an agent's API stream.

The restart did one useful thing by accident: it also closed connections the new policy no
longer allowed. A blind tunnel (a `passthrough` host) is decided once, at its TLS handshake, and
never looked at again. The same was never true of `fy allow remove`, which has always been
applied without a restart and so left a revoked host's open tunnels running.

## Decision

1. **Posture travels in a live file, not the launch environment.** The proxy's spec carries only
   what never changes with posture (log path, allowlist path, the live file's path, `host.env`'s
   path, the port). The rules, the wall switch and the passthrough list go in `spec["live"]`; the
   supervisor writes it each tick when it changed — whole, renamed into place — and logs
   `… settings updated — … (no restart)` when the change reaches a running proxy.
2. **The addon re-reads it** at the top of every hook and once a second (an idle proxy sees no
   hooks). A rule whose spec and secrets are unchanged keeps its object and so its cached token;
   a new rule warms off the request path at WARN, as at startup. An unreadable file fails closed:
   no rules, the wall enforcing, nothing tunnelled blind.
3. **Secrets are read by name from `host.env`.** Each rule already declares the names its minter
   may read (`InjectRule.env`). The addon resolves them itself — the operator's own exports
   first, as before, then `host.env` — and re-resolves when the file changes, dropping a rotated
   rule's cached token. The secret stamp is gone.
4. **The proxy runs without `host.env` in its environment.** The supervisor merges every axis's
   secret into its own environment; a daemon whose spec asks (`scrub_host_env`) is launched
   without the keys that came from `host.env`. Before this, all of them sat in mitmdump's process
   environment.
5. **A narrowing closes what it no longer allows — and only that.** The addon remembers each
   CONNECT that got through. After any policy change (the live file or the allowlist) it closes
   the connections whose CONNECT the wall would now refuse, and the blind tunnels it would now
   decrypt (the host left `passthrough`, or an injector now owns it). Widening closes nothing; a
   build tunnel stays open while its host is allowed. A revoked grant now closes its tunnels too.
6. **Keychains are not part of this.** A `security`/`secret-tool` entry is readable by any process
   running as the operator through the same CLI, so it adds at-rest protection (backups) but no
   per-process isolation, at the cost of a second backend on every host without a Secret Service
   (headless Linux, WSL2, CI). If a secret source is ever wanted, the seam is a minter kind
   (`static_token.py` notes it), chosen per secret.

## Consequences

- **A mode switch, a learn window ending, an adopted `passthrough` and a token rotation no longer
  touch a running connection.** The proxy restarts only when its own code, its port or mitmdump
  itself changes. Pinned by `test_a_posture_change_leaves_the_proxy_launch_settings_alone` and,
  on the wire, `test_a_download_in_flight_survives_a_settings_change`.
- **Closing a connection relies on mitmproxy internals.** mitmproxy has no public API to close a
  connection without a flow (checked on 12.2.3 and on `main`, 2026-09-23), and a blind tunnel has
  no flow by construction. The addon reaches `proxyserver.connections[client.id]` and calls the
  handler's `close_connection`. A failure is logged at WARN and leaves the tunnel open, so a
  mitmproxy upgrade that moves these must be caught by `test_proxy_reload_e2e.py`, which fails
  without the close (mutation-checked).
- **`requires` gates only the proxy's first launch.** A rule that turns on later, with a
  prerequisite missing, can't stop a proxy that is already running; its mints fail at WARN, one
  host degraded, which is the outcome `requires` is kept credential-free to protect. The doctor
  rows still name what is missing.
- **The proxy's `started …` line no longer names the current posture.** The settings-updated line
  does, per change; `fy host logs` shows both.

## Rejected

- **Drain, then restart.** Wait for open connections to finish, bind the new process, stop the
  old one. The handover still refuses connections for a moment on a single port (mitmdump can't
  take over a listening socket), keep-alive clients and streams rarely let the count reach zero,
  and while it waits the old posture stays in force — a credential switched off keeps being
  injected. About as much code as the reload, with a worse result.
- **Restart only for narrowing changes.** Simpler than closing tunnels selectively, but every
  narrowing (the wall going up, a learn window ending) cuts everything again, and revoked grants
  would still leave their tunnels open.
- **Write secret values into the live file.** One reader instead of two, but a second plaintext
  copy of `host.env` on disk, to protect exactly as well as the first.
