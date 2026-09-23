# Egress, capture and the wall — why a request failed, and who can unblock it

All of the box's internet traffic routes through a proxy **on the host**. That's how credential
injection works, and it's the observation point for everything leaving the yard. Three separate
controls sit on that path; they get conflated constantly, so name the right one when you ask.

| control | question it answers | who changes it |
| --- | --- | --- |
| **routing** | does traffic go through the proxy at all? | baked in at box creation (always on) |
| **the wall** | can traffic *ignore* the proxy and go direct? | project config, at VM provisioning |
| **the allowlist** | which hosts may the proxy reach? | `fy allow …` on the host, live |
| **passthrough** | which hosts are tunnelled *without* decryption? | `[proxy] passthrough` in `foldyard.toml`, adopted on the host |

## Diagnosing a failed request

- **`403` from the proxy / "blocked by the egress wall"** — the allowlist refused the host. It's a
  decision. Ask the human for `fy allow add <host> --level session` (or `--level permanent` if it's
  a permanent dependency of the project). Grants live in the host's home, outside the mount, so
  there is deliberately no way to grant one from here. If the host is a real dependency of the
  PROJECT (not just this task), also add it to `[proxy] recommend` in `foldyard.toml` with a
  one-line `why`: that commits the recommendation for the whole team — each operator is OFFERED
  it per host (after reviewing your edit at the adoption gate) instead of rediscovering the
  block, and nothing is granted without their yes.
- **Nothing is blocked, but the operator mentions a "learn window"** — the wall is observing for
  a while (`fy allow wall learn`): requests pass, and every host that WOULD be refused is recorded
  for the operator to review. It enforces again by itself when the window ends, so a host that
  works now may be refused later if the operator doesn't grant it — say which hosts your task
  needed.
- **Connection refused / hangs on everything** — the host proxy probably isn't running. `fy doctor`
  names it; the human checks it with `fy host` and restarts it with `fy host restart`, on the
  host (the box can't).
- **TLS/certificate errors** — the proxy re-signs decrypted traffic with a CA the box trusts, so a
  tool with its own hardcoded trust store (or one that pins) can fail here. Point it at the box's
  combined CA bundle rather than disabling verification.
- **Works for a while, then dies ~60s in** — that's an idle-connection reset from the upstream,
  not the proxy. Streaming or keepalives fix it; the proxy doesn't.

## Decryption is not filtering

The proxy always decrypts and logs requests so the human can *see* what the yard talks to. That
doesn't block anything, and hosts on the project's `passthrough` list are tunnelled without
decryption — often a couple of hundred of them, since one `@bundle` reference expands to a whole
toolchain. `fy config widenings` prints the real count. (There is no `capture` switch any more:
decryption is always on.)

Conversely the wall (`[machine] wall = true`) is the enforcing control: traffic that ignores the
proxy environment is *rejected*, not silently allowed. Without it, routing is cooperative — it
bounds what is **logged**, not what can leave. `fy verify` probes this directly.

## Don't route around it

Unsetting `HTTPS_PROXY`, adding `--insecure`, or hunting for a direct path defeats the observation
the human relies on — and under the wall it simply fails. If a task genuinely needs a host, say
which host and why; that's a one-command grant for them.

Depth: `fy docs networking`, `fy docs security`, `fy docs adr-0009` (why monitoring is called
monitoring and not filtering).
