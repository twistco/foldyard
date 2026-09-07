# Networking — the egress proxy, capture, and the wall

The box routes its internet traffic through a proxy on your host. This page is how
that works day to day: what gets logged, how to watch and control it, and — the part that
matters for trust — which guarantees are cooperative and which are enforced.

## How egress flows

The box routes through an always-on `mitmdump` proxy running on the host (declared
by the `[proxy]` table; `fy up` launches the host supervisor for you, `fy host` runs it in
the foreground). How strong "routes" is depends on the backend: only Lima with
`[machine] wall = true` **enforces** that all box traffic goes through the proxy; on Podman
(or with the wall off) the routing is **cooperative** — honoured by well-behaved software,
bypassable by anything that ignores proxy env vars (see "Blocking, not just watching" below).
Routing rides the standard proxy env (`HTTPS_PROXY` etc.) plus a CA the box
trusts, both injected at box-up:

- the proxy CA is installed into the box's **system trust store** (additive — curl, wget,
  apt all verify) and `NODE_EXTRA_CA_CERTS` stays additive;
- `REQUESTS_CA_BUNDLE`/`GIT_SSL_CAINFO` point at a **combined** bundle (system roots + the
  proxy CA), so both passed-through real certs and MITM'd certs verify.

Each project — and each worktree — gets its own proxy port from a cross-project **port band**
registry (`~/.foldyard/ports.json`), so two stacks never share a listener and a token minted
for one worktree can't be read through another's proxy.

In-stack traffic (your services talking to each other by container name) never touches the
proxy — it's on `NO_PROXY` and stays inside the VM.

## The log

Every proxied request lands in `~/.foldyard/<project>/logs/egress.jsonl` — outside the repo
mount on purpose, so nothing inside the VM can read or rewrite its own audit trail. Watch it
in the **Network Log** tab of `fy tui` (grouped by host) or `tail -f` the JSONL.

The log is bounded: it rotates past `PROXY_LOG_MAX_BYTES` (default 5 MiB) into dated backups,
pruned to `PROXY_LOG_BACKUPS` (default 5). The TUI reads only a bounded tail
(`FOLDYARD_LOG_TAIL_BYTES`), so a large log never slows the panel.

## What gets decrypted: the `capture` axis

`capture` is a host-side decision about what the always-on proxy *does* — flipping it never
needs a box recreate:

- **`capture=off`** (default): TLS **passthrough** — real certificates end-to-end, the log
  gets host-level rows (SNI, no request bodies). Hosts an injector owns (say
  `api.github.com` under a GitHub posture) are still decrypted so their auth header can be
  rewritten.
- **`capture=on`** (`fy mode capture=on`): full MITM — every request logged with method and
  path — *except* hosts on your `[proxy] passthrough` list, which stay tunnelled and
  SNI-logged. Entries are exact hosts, `*.suffix` globs, or `@bundle` refs (`@anthropic`,
  `@vcs`, `@node`, … — `@all` is the default), so "capture on" means *MITM the unexpected
  egress, leave the trusted toolchain fast and quiet*.

  A host on that list is exempt from the very monitoring this section is about, so `passthrough`
  is not read from your checkout while the yard runs: like the rest of `foldyard.toml`, the host
  uses the copy you **adopted**, and an edit takes effect when you accept it at the next
  `fy up`/`fy host` (`fy config diff|adopt|revert`; [ADR-0022](./adrs/0022-host-runs-the-adopted-config.md)).

No credential is involved either way — capture composes with, but is independent of, the
injector postures (see [modes.md](./modes.md)).

## Blocking, not just watching: `default_deny` and the wall

Two separate controls, often confused:

**The allowlist**: when enforcement is on, the *proxy* refuses any host that isn't granted.
`fy allow wall on|off` is the live switch; `[proxy] default_deny` only SEEDS it, for the first run
before the host store has an answer (`init` starts you at `true` — the scaffold's `recommend`
entries carry its bootstrap through, and `fy allow wall off` drops to observe-only while you're
still learning a new dependency's egress). Grants are never repo config either: every level lives
in the host-side allow-store, added with `fy allow add <host> [--level once|session|permanent]`,
`fy allow sync [--yes]` for the recommended set, or one keypress on a blocked row in `fy tui`'s
Network Log. Both live host-side for the same reason — repo config is writable from inside the box,
so a committed wall is one the yard can widen, or switch off. Keyless hosts are always allowed
implicitly — never list them.

**The wall** (`[machine] wall = true`, Lima backend — the `init` default) makes the proxy the
*only way out*. It provisions a fail-closed nftables firewall into the VM itself, so software
that ignores proxy env vars — malware, static Go binaries, raw sockets — gets nothing instead
of a direct route.

The honesty rule that separates them: **without the wall, routing is cooperative.** Proxy env
vars are honoured by well-behaved software and ignorable by anything else. So:

- `backend = "lima"` + `wall = true` — egress control is **enforced**. This is the default.
- `backend = "podman"` (the single shared machine) — no in-VM wall exists; the proxy sees
  everything cooperative, and that's a *monitoring* guarantee, not a filtering one.
- One gap remains even with the wall on the proxy path: a client speaking **QUIC/HTTP-3
  (UDP/443)** bypasses a CONNECT proxy entirely. The wall's default-deny covers it; on
  cooperative-only setups it's simply not captured.

## When something's off

- **"I flipped capture on and see nothing."** A box created before the always-route era has
  neither the routing env nor the CA — recreate it once (`fy box down && fy box up`). The
  Network Log only rows a request once it gets a response, so a failing TLS handshake (CA
  not trusted) shows nothing.
- **Every request in the box suddenly refuses to connect.** The box routes through the proxy,
  so a dead proxy means no egress — `fy doctor` has an "egress proxy" check for exactly this,
  and `fy up` relaunching the supervisor is the usual fix.
- **A build needs a host you haven't allowed.** Watch the TUI's blocked rows and grant from
  there (`a`), or `fy allow add <host> --level permanent`. Grants live host-side, never in repo
  config — the box must not be able to widen its own wall.

## Deeper

Why transparent capture is hard on rootless podman (stacked userspace netstacks), the
forcing-function options that were weighed and the monitoring-vs-enforcement split are recorded
in [ADR-0009](./adrs/0009-monitoring-cooperative-enforcement-locked.md). The wall's design and
implementation live in [lima-wall-machine-integration.md](./lima-wall-machine-integration.md);
how nono-style kernel sandboxes relate is in [prior-art.md](./prior-art.md).
