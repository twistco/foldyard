# Networking — the proxy, the allowlist, and the firewalls

The box sends its internet traffic through a proxy on your computer. This page covers how that
works day to day: what gets logged, how to watch and control it, and — the part that matters for
trust — which guarantees are cooperative and which are enforced. Terms are defined in the
[glossary](./glossary.md).

## How traffic leaves the box

The box routes through an always-on `mitmdump` proxy on your computer, configured by the
`[proxy]` table. `fy up` / `fy box up` start the supervisor that runs it; `fy host` shows its
status.

Routing uses the standard proxy environment variables (`HTTPS_PROXY` etc.) plus a CA certificate
the box trusts, both set when the box is created:

- the proxy's CA is added to the box's **system trust store** (curl, wget and apt all verify
  against it), and `NODE_EXTRA_CA_CERTS` adds it to Node's roots;
- `REQUESTS_CA_BUNDLE`, `GIT_SSL_CAINFO` and `SSL_CERT_FILE` point at a **combined** bundle
  (system roots + the proxy CA), so both decrypted and passed-through connections verify.

How strong "routes" is depends on your setup. Only the lima backend with `[machine] firewall =
true` **enforces** that all traffic goes through the proxy. Without the VM firewall, routing is
**cooperative**: well-behaved software honours the proxy variables, anything that ignores them
goes around. See [Blocking, not just watching](#blocking-not-just-watching).

Each project — and each worktree — gets its own proxy port from a cross-project port registry
(`~/.foldyard/ports.json`). Two stacks never share a listener, and a token meant for one worktree
can't be read through another's proxy.

Traffic between your services (by container name) never touches the proxy: it's on `NO_PROXY`
and stays inside the VM.

## The log

Every proxied request is logged to `egress.jsonl` in that worktree's state folder on your
computer (`~/.foldyard/<project>/main/logs/` for the main checkout). It is outside the repo on
purpose, so nothing in the VM can read or rewrite it. Watch it in the **Network Log** tab of
`fy tui` (grouped by host), or `tail -f` the file.

The log is bounded:

- past `PROXY_LOG_MAX_BYTES` (default 5 MiB) it rotates into dated backups;
- the newest `PROXY_LOG_BACKUPS` (default 5) backups are kept;
- the TUI reads only the last `FOLDYARD_LOG_TAIL_BYTES` (default 256 KiB), so a big log never
  slows it down.

## What gets decrypted: everything but the trusted toolchain

The proxy decrypts and logs every request — method, host, path, status — *except* hosts on your
`[proxy] passthrough` list. Those are tunnelled with their real certificates and logged as one
row per connection (the hostname, no path).

Entries are exact hosts, `*.suffix` globs, or `@bundle` names (`@anthropic`, `@vcs`, `@node`, …).
The default is `@all`, every bundle: decrypt unexpected traffic, leave the trusted toolchain fast
and quiet. A host whose credential the proxy injects (say `api.github.com` with `github=app`) is
always decrypted, whatever the list says, so its auth header can be replaced.

Decryption can't be switched off ([ADR-0029](./adrs/0029-the-proxy-always-decrypts.md)): it costs
about 3 ms per new connection and caps one checkout's decrypted throughput around 600 MB/s,
and seeing paths is what makes the log useful. A host that breaks
under decryption (it pins its certificate, needs a client certificate, or ships its own trust
roots) goes on the `passthrough` list instead.

Because a `passthrough` host escapes this monitoring, your computer doesn't read the list from the
checkout while the VM runs. Like the rest of `foldyard.toml`, it uses the copy you **adopted**. An
edit takes effect when you accept it at the next `fy up` or `fy host restart` (`fy config diff`,
`fy config adopt`, `fy config revert`; [ADR-0022](./adrs/0022-host-runs-the-adopted-config.md)).

Decryption involves no credential. It works alongside the credential switches (see
[modes.md](./modes.md)) but doesn't depend on them.

## Blocking, not just watching

Three separate controls, often confused:

| control | where it runs | what it does | set by |
| --- | --- | --- | --- |
| **the allowlist** | the proxy, on your computer | refuses hosts you haven't granted | `fy allow enforce on\|off\|learn` (`[proxy] enforce` seeds it) |
| **the VM firewall** | inside the VM | refuses any traffic not going through the proxy | `[machine] firewall = true` (lima backend) |
| **the host firewall** | on your computer (Linux) | refuses the VM process's own traffic, except to the proxy | `[machine] host_firewall = true` |

### The allowlist

When enforcement is on, the proxy refuses any host that isn't granted. `fy allow enforce
on|off|learn` is the switch. `[proxy] enforce` in `foldyard.toml` only sets the answer for the
first run, before your computer has one of its own.

**Learn first, then enforce.** `fy init` sets `enforce = "learn"`. The first `fy box up` / `fy up`
opens a one-hour window where:

- nothing is refused;
- the proxy records every host it *would* have refused, with the User-Agent that asked
  (`npm/10.8.2`, `uv/0.8`, `git/2.45`), so you can see which tool wanted it;
- when the window ends, enforcement turns on **by itself**.

Then `fy allow learn` lists what was recorded, grants it in one go, and prints `[proxy] recommend`
lines you can commit to share the hosts with your team. Each line's `why` is what the box was
*seen* doing (`observed: npm/10.8.2 GET /react, /@types/node (+4) — edit me`), taken from the
User-Agent and the first path segments of the requests (never a query string). That text comes
from box traffic: replace it with the real reason before committing, since teammates read it
when asked to grant the host.

For a new dependency, open another window with `fy allow enforce learn --for 30m`. Prefer that to
`fy allow enforce off`, which also only watches but stays off until someone remembers to turn it
back on.

**Grants** are stored on your computer, never in repo config:

- `fy allow add <host> [--level once|session|permanent]` (default `session`);
- `fy allow sync [--yes]` for the hosts the repo recommends;
- one keypress on a blocked row in `fy tui`'s Network Log.

Both the grants and the enforcement switch live on your computer for the same reason: the box can
write repo config, so an allowlist in the repo is one the box could widen or switch off. Keyless
agent hosts are always allowed — never list them.

### The VM firewall

`[machine] firewall = true` (lima backend; `fy init` turns it on) makes the proxy the *only way
out*. It sets up nftables rules inside the VM, as root at boot, that refuse everything else. So
software that ignores proxy variables — malware, static Go binaries, raw sockets — gets nothing
instead of a direct route.

**Without the VM firewall, routing is cooperative.** Proxy variables are honoured by well-behaved
software and ignored by anything else:

- `backend = "lima"` + `firewall = true` — traffic control is **enforced**. This is the default.
- `backend = "podman"` (one shared VM) — no VM firewall is possible. The proxy sees everything
  that cooperates: a *monitoring* guarantee, not a filtering one.
- **QUIC/HTTP-3 (UDP/443)** bypasses a CONNECT proxy entirely. With the VM firewall, it is
  refused. Without it, it is not captured at all.

### The host firewall

`[machine] host_firewall = true` (Linux only; needs `firewall = true`) enforces the same rules
again on your computer, around the VM process itself — so even someone who took over the VM's
kernel still can't get out. foldyard never installs it for you: `fy machine host-firewall` prints
the rules and the `sudo` commands, you run them once, and every `fy up` checks they are in effect
([ADR-0028](./adrs/0028-no-elevation-on-the-host-operator-applies.md)).

### Image builds: trusted, still behind the allowlist

With the VM firewall on, image builds (`fy box build`, and the stack build `fy up` runs) also go
out through the proxy. How that works:

- **Tunnelled, not decrypted.** A build container doesn't have the proxy's CA, so foldyard gives
  the build a proxy URL with a marker (`fy-build`), and the proxy tunnels those connections. The
  log shows them as `tls tunnel` rows flagged `build`.
- **The allowlist still applies.** The marker changes what is decrypted, never what is allowed
  ([ADR-0029](./adrs/0029-the-proxy-always-decrypts.md)).

When the allowlist refuses a build, the tool's error names the URL it *asked* for, which is often
not the host that was refused: a CDN can redirect inside the tunnel (Playwright's
`cdn.playwright.dev` sends browser downloads to `storage.googleapis.com`). So foldyard reports the
refused hosts itself:

1. **On your computer, with a terminal,** it asks about each refused host: once (15 minutes, long
   enough for the build), session, permanent, or no.
2. It builds again. The layer cache makes that cheap. A redirect chain reveals one more hop per
   attempt, up to 4 attempts.
3. It offers to add what you granted to `foldyard.toml`'s `[proxy] recommend` as `when = "build"`
   entries (keeping the file's comments). If the checkout matched the config your computer runs,
   the edit is adopted at once; otherwise it waits for the adoption prompt at the next `fy up`.
   Each `why` records what was *seen*: reword it before you commit.
4. **Without a terminal,** it prints the `fy allow add … --build` commands instead.
5. **Inside the box,** it does nothing: grants are made on your computer.

**Build grants are for builds only.** The box can't use them, so the box's allowlist stays as
narrow as before. A build proves it is one with a secret foldyard creates for that build (the
password in its proxy URL, stored on your computer only as a hash, revoked when the build ends).
The public `fy-build` marker alone still gets a build tunnelled, but unlocks no build grant, so
neither the box nor a build started inside it can use one. `fy allow add <host> --build` makes
the same kind of grant by hand, and `fy allow list` shows build grants separately.

`when = "build"` recommendations aren't offered at `fy up` (every launch would ask about a host
only a build needs). A teammate is offered them, with their `why`, when their own build is
refused.

## When something's off

- **The Network Log is empty.** The box may be missing the proxy variables or the CA — recreate
  it once (`fy box down && fy box up`). The log only shows a request once it gets a response, so
  a failing TLS handshake (CA not trusted) shows nothing.
- **Every request in the box suddenly refuses to connect.** The box routes through the proxy, so
  a dead proxy means no traffic out. `fy doctor` has an "egress proxy" check for this, and `fy up`
  restarting the supervisor is the usual fix.
- **A build or tool needs a host you haven't allowed.** Watch the TUI's blocked rows and grant from
  there (`a`), or run `fy allow add <host> --level permanent`. Grants live on your computer, never
  in repo config, so the box can't widen its own allowlist.

## Deeper

- Why transparent capture is hard on rootless podman, the options weighed, and the
  monitoring-vs-enforcement split:
  [ADR-0009](./adrs/0009-monitoring-cooperative-enforcement-locked.md).
- The VM firewall's design:
  [lima-wall-machine-integration.md](https://github.com/twistco/foldyard/blob/main/docs/lima-wall-machine-integration.md).
- How kernel sandboxes like nono relate:
  [prior-art.md](https://github.com/twistco/foldyard/blob/main/docs/prior-art.md).
