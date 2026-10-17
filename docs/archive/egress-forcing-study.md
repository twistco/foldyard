# Forcing all egress through the proxy — the 2026-06 study

> **What it was:** three linked investigations from 2026-06-16 to 06-25 into one question: can
> foldyard make the proxy the *only* way out of the VM, rather than the way out for software that
> agrees to use it? A capture study (06-16), a Lima spike (06-24), and two runnable proof kits
> built on a native Lima VM (06-25). **Status:** finished. The answer shipped as the VM firewall
> (validated on a Linux host 2026-09-11, in CI since 2026-09-17) and, on Linux, the host firewall
> (2026-09-18). **Superseded by:** [networking.md](../networking.md#blocking-not-just-watching),
> [lima-wall-machine-integration.md](../lima-wall-machine-integration.md),
> [ADR-0009](../adrs/0009-monitoring-cooperative-enforcement-locked.md),
> [ADR-0028](../adrs/0028-no-elevation-on-the-host-operator-applies.md) and
> [ADR-0029](../adrs/0029-the-proxy-always-decrypts.md). The kits live on in
> [lima-network-forcing-kit/](./lima-network-forcing-kit/).

This page keeps what is still useful: the options weighed, what TLS interception costs, and the
trade-off that decided the design. Names from the time (the `capture` switch, the `fy wall` verb,
`[machine].wall`) are retired; see [What is superseded](#what-is-superseded).

## The starting point: cooperative capture

The box already routed through a proxy on your computer, using `HTTPS_PROXY` and a CA certificate
it trusted. That catches every client that honours proxy variables, and nothing else. Clients that
ignore them include Go programs that never read the variables, gRPC, raw sockets, anything that
speaks QUIC (HTTP/3 over UDP, which a `CONNECT` proxy can't carry), and malicious code that simply
unsets `HTTPS_PROXY`. So capture was **visibility, not enforcement** — the rule ADR-0009 later
wrote down.

Three facts made "just force it" hard:

- **No host-side hook on macOS.** Rootless podman machine stacked two userspace network stacks:
  pasta (rootless containers' networking) inside the VM, then gvproxy (podman machine's network
  relay) on your computer. By the time a packet reached your computer
  it had already become an ordinary socket owned by gvproxy, so there was no per-container packet
  path to redirect. gvproxy offers packet capture, a small DNS server and port forwards; none of
  them is a proxy.
- **No firewall on the default rootless path.** pasta turns a container's traffic into the VM
  user's own sockets, with no NAT and no firewall hook, so the textbook "redirect port 443 to the
  proxy" rule had nothing to attach to.
- **127.0.0.1 inside the VM is the VM.** A proxy listening on your computer's loopback is
  unreachable from the box; it has to be addressed through the VM's gateway. The proxy-settings
  bugs reported against desktop container tools at the time were all this class: getting proxy
  variables to the right process. Setting the variables directly avoids the bugs, not the limit.

**Process sandboxes don't transfer.** Tools that wrap one process tree (Landlock on Linux, Seatbelt
on macOS) lock `connect()` in the kernel so the process can reach only its proxy. That lives in the
same kernel as the process it wraps, so it can't wrap a VM from outside, and it covers one process
tree, not a stack of services. The VM-level equivalent of that kernel lock is a default-deny
firewall.

## The options weighed

| | how traffic is forced | catches clients that ignore the proxy? | verdict |
| --- | --- | --- | --- |
| **A** | proxy variables only | no | shipped first; still how routing works |
| **B** | a gateway container whose network the services share | yes | rejected: services sharing one network lose their own ports and identity |
| **C** | a named podman network, with a redirect rule in its namespace | yes | rejected: most moving parts, fights the default network |
| **D** | a firewall inside the VM | yes | **chosen**, once Lima made it easy |
| **E** | passive packet capture | sees packets, can't read TLS | **never built**; still a good leak detector |

Option D looked weak at first: podman machine's VM image is immutable and configured once at
creation, so rules wouldn't persist. The Lima spike changed that. No Lima network stack gives a
cleaner host-side hook than gvproxy, but Lima lets you **own the VM's provisioning**: a root
script that runs at boot, before any user logs in. That is the real enabler — a root-owned
firewall on the VM's only way out, applied before the agent exists. (Firewall rules also need a
systemd unit to survive a reboot; a one-off script is not enough.)

Option E is the one idea left open. Recording packets at the VM's edge and comparing them with the
proxy's log would answer "did anything get out without going through the proxy?" — an audit that
needs no CA and no cooperation. With the VM firewall on, the answer should always be "nothing";
packet capture would prove it rather than assume it. Nothing in foldyard does this today.

## What TLS interception costs

Decrypting traffic needs the box to trust the proxy's CA, and trust is per runtime, not per
machine. The system store covers curl and most native tools; Node ignores it
(`NODE_EXTRA_CA_CERTS`); Python's `requests` reads certifi's bundle rather than the system store
(`REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`); git, the AWS CLI and gcloud each have their own variable;
Java has its own keystore; browsers have their own NSS store.

Even with every store set, some traffic **breaks under decryption whatever you trust**:

- clients that pin a certificate;
- clients that ship their own trust roots (Java, some static Go binaries);
- upstreams that require a client certificate (mutual TLS);
- QUIC/HTTP-3, which a `CONNECT` proxy can't carry at all — block UDP/443 and clients fall back to
  TCP, where the proxy sees them.

foldyard today sets the variables in the box, gives stack containers and build steps the CA when the
VM firewall is on, and keeps `[proxy] passthrough` as the escape hatch for hosts that break
([networking.md](../networking.md#what-gets-decrypted-everything-but-the-trusted-toolchain)).

## Two proofs, one trade-off: rootful is an escape, rootless is the moat

The kits were run on a plain Lima VM on macOS, not nested inside the dev VM: the first attempt
nested a guest inside the dev VM, and it starved the outer VM until nothing could be driven
([nested-virt.md](../nested-virt.md) keeps the warning).

1. **The uid firewall (rootless).** Default-deny all egress from one unprivileged user, with an
   explicit proxy as the only way out. Everything that user starts — processes, rootless
   containers, even `--network host` containers — leaves the kernel tagged with that user's id,
   so one rule catches all of it. A client that ignores the proxy just *fails*: fail-closed, not
   transparent.
2. **The transparent firewall (rootful).** Redirect the container subnet's ports 80 and 443 to the
   proxy and refuse everything else, so clients need no proxy settings at all. It even caught a
   sibling container started through the engine socket. But it needs rootful podman, so containers
   sit on a bridge the firewall can hook.

The deciding finding: **with rootful podman, the engine socket is VM-root.** An agent that can
start a `--privileged` container can flush the firewall, or run on the host network and skip it.
The agent needs the engine to do its job, so engine access is itself the escape hatch, and the
firewall has to sit where that engine can't reach it. Rootless removes the route to VM-root, so the
uid firewall holds; the transparent one buys convenience by giving up exactly that. foldyard took
the uid firewall. And since no firewall inside a VM survives an attacker who *does* reach VM-root,
the backstop has to sit outside the VM: credentials that never enter it, and, on Linux, the host
firewall.

## Small findings worth keeping

- **Redirect in PREROUTING, not OUTPUT.** On the Lima/Ubuntu guest, a `REDIRECT` rule in the
  nftables OUTPUT hook (for the VM's own processes) silently did nothing; in PREROUTING (for
  forwarded container traffic) it worked. It cost about six debugging rounds, and is why the uid
  kit uses an explicit proxy instead of a transparent one.
- **Don't trust image defaults; mask anything that listens or grants.** The Ubuntu cloud image
  the first kit used shipped the rootful `podman.socket` *enabled* — a ready-made route to
  VM-root, caught only because a check looked for it. The kit moved to a minimal Debian image and
  masked the socket in provisioning anyway. foldyard's boot script masks it on every VM with the
  firewall on, and the host refuses such a VM where it is enabled again.
- **Refuse, don't drop.** Refusing with a reset gives an instant "connection refused" and a log
  line; silently dropping gives a hang that looks like a network fault.

## What was built

- **The VM firewall** (`[machine] firewall`, Lima only; [lima-wall-machine-integration.md
  §2](../lima-wall-machine-integration.md)): the uid kit's shape on foldyard's real VM.
  Default-deny for the VM user's id *and* the extra user ids its rootless containers run as; open
  only loopback, DNS to local resolvers, and the proxy's port range on your computer; a forward-chain refusal for any bridged
  container; the rootful socket masked. It is installed as root at boot from a script foldyard
  records in the VM's config, and the host reads back what the guest applied, failing closed on a
  mismatch. The proxy stayed on your computer: running it inside the VM would have pulled
  credentials into the VM the agent lives in.
- **The host firewall** (`[machine] host_firewall`, Linux only; ADR-0028): the same rule again on
  your computer, matching the VM's own process by the kernel control group (cgroup) it runs in,
  so even VM-root can't get out. This
  partly reverses the study's "no host-side hook" finding — the hook is the VM process, not a
  per-container path. foldyard renders the rules and prints the `sudo` commands, you run them
  once, and every `fy up` probes that they are in effect. Not on macOS, and not on WSL2 (its stock
  kernel lacks the needed nftables support).
- **The allowlist** (`fy allow`): the proxy refusing hosts you haven't granted. Cooperative on its
  own; enforcement once the VM firewall makes the proxy the only way out.
- **Always decrypt** (ADR-0029): the proxy decrypts everything but `[proxy] passthrough`, so the
  TLS costs above are paid once, with one escape hatch.

## What is superseded

- The `capture` switch and its "decrypt nothing" default: removed by ADR-0029.
- The `fy wall` verb and its separate proof VM: retired when the VM firewall landed on the real VM.
- `[machine].wall`, `host_wall`, `[proxy] default_deny`: now `[machine] firewall`,
  `host_firewall` and `[proxy] enforce` (old names still accepted).
- Permanent grants written to `foldyard.toml`: grants live only on your computer at every level
  ([ADR-0023](../adrs/0023-no-host-executed-code-from-the-repo-mount.md)).
- A fixed proxy port: every project and worktree has its own port range.
- Options B and C: not pursued, since the VM firewall reaches the same guarantee with less plumbing.

Still open: Option E, the packet-capture leak detector; and without the VM firewall, QUIC and any
client that ignores proxy variables still go uncaptured.

## Sources

- INNOQ, ["I sandboxed my coding agents. Now I control their
  network"](https://www.innoq.com/en/blog/2026/03/dev-sandbox-network/) — the Lima + nftables
  default-deny blueprint the kits followed.
- [Nono](https://github.com/always-further/nono) and Anthropic's
  [sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime) — the process-level
  approach that doesn't transfer to a VM.
- [gvisor-tap-vsock](https://github.com/containers/gvisor-tap-vsock) (gvproxy),
  [passt/pasta](https://passt.top/passt/about/), and mitmproxy's
  [transparent mode](https://docs.mitmproxy.org/stable/howto/transparent/).
