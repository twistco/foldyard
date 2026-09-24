# Egress — why a request failed, and who can unblock it

All of the box's internet traffic routes through a proxy **on the human's computer** (the host).
That's how credential injection works, and it's where everything leaving the VM is observed.
Several separate controls sit on that path; they get conflated constantly, so name the right one
when you ask. (`fy docs glossary` defines each.)

| control | question it answers | who changes it |
| --- | --- | --- |
| **routing** | does traffic go through the proxy at all? | baked in at box creation (always on) |
| **the VM firewall** (`[machine] firewall`) | can traffic *ignore* the proxy and go direct? | project config, applied when the VM boots |
| **the allowlist** | which hosts may the proxy reach? | `fy allow …` on the host, live |
| **passthrough** (`[proxy] passthrough`) | which hosts are forwarded *without* decryption? | `foldyard.toml`, adopted on the host |

## Diagnosing a failed request

- **`403` from the proxy / a "blocked" message** — the allowlist refused the host. It's a
  decision. Ask the human for `fy allow add <host> --level session` (or `--level permanent` if
  it's a lasting dependency of the project). Grants live in the host's home, outside the mount, so
  there is deliberately no way to grant one from here. If the host is a real dependency of the
  PROJECT (not just this task), also add it to `[proxy] recommend` in `foldyard.toml` with a
  one-line `why`: that commits the recommendation for the whole team — everyone is *offered* it
  per host on their own computer (after reviewing your edit at the adoption gate) instead of
  rediscovering the block, and nothing is granted without their yes.
- **Nothing is blocked, but the human mentions a "learn window"** — the allowlist is observing
  for a while (`fy allow enforce learn`): requests pass, and every host that *would* be refused is
  recorded for them to review (`fy allow learn`). It enforces again by itself when the window
  ends, so a host that works now may be refused later if it isn't granted — say which hosts your
  task needed.
- **Connection refused / hangs on everything** — the proxy probably isn't running. `fy doctor`
  names it; the human checks it with `fy host` and restarts it with `fy host restart`, on their
  computer (the box can't).
- **TLS/certificate errors** — the proxy re-signs decrypted traffic with a CA the box trusts, so a
  tool with its own hardcoded trust store (or one that pins) can fail here. Point it at the box's
  combined CA bundle rather than disabling verification. A host that can't work under decryption
  at all (pinned certificates, client certificates) belongs in `[proxy] passthrough`.
- **Works for a while, then dies ~60s in** — that's an idle-connection reset from the upstream,
  not the proxy. Streaming or keepalives fix it; the proxy doesn't.

## Decryption is not filtering

The proxy always decrypts and logs requests so the human can *see* what the VM talks to. That
doesn't block anything, and hosts on the project's `passthrough` list are forwarded without
decryption — often a couple of hundred of them, since one `@bundle` reference expands to a whole
toolchain. `fy config widenings` prints the real count.

The VM firewall (`[machine] firewall = true`) is what makes routing enforced: traffic that ignores
the proxy environment is *rejected*, not silently let out. Without it, routing is cooperative — it
bounds what is **logged**, not what can leave. `fy verify` probes this directly.

## Don't route around it

Unsetting `HTTPS_PROXY`, adding `--insecure`, or hunting for a direct path defeats the observation
the human relies on — and under the VM firewall it simply fails. If a task genuinely needs a host,
say which host and why; that's a one-command grant for them.

Depth: `fy docs networking`, `fy docs security`, `fy docs adr-0009` (why monitoring is called
monitoring and not filtering).
