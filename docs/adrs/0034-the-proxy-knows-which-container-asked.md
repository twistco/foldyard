# ADR-0034 — The proxy knows which container asked: an in-VM forwarder attributes egress

- **Status:** Proposed (2026-10-22). Not implemented. The spike ran on 2026-10-22/23
  ([forwarder-spike.md](https://github.com/twistco/foldyard/blob/main/docs/forwarder-spike.md)): the forwarder is **viable with caveats, with no
  measurable performance penalty**. The caveats are carried into the Decision below, and the
  fallback (a per-container secret in each proxy URL) is not preferred. Awaiting acceptance.
- **Sources:** the 2026-10-22 investigation of a proxy stall during parallel BashArena image
  builds (the egress log, `lsof` on the host, a replay batch's monitor digest; not in the repo); the
  spike record [forwarder-spike.md](https://github.com/twistco/foldyard/blob/main/docs/forwarder-spike.md) (a real Lima `template:podman` guest:
  Fedora 44, podman 5.8.9, netavark 1.17.2, pasta, vz on arm64); a read-only audit of how the
  maintainers' own consumers use injection (2026-10-23, not in the repo). Related:
  [0002](./0002-stack-colocation-project-as-isolation-unit.md) (the project is the unit
  of isolation), [0007](./0007-credential-injection-at-egress-proxy.md) and
  [0008](./0008-keyless-agent-auth.md) (injection at the proxy),
  [0009](./0009-monitoring-cooperative-enforcement-locked.md) (nothing in the guest holds against
  VM-root), [0016](./0016-per-worktree-posture.md) (per-worktree proxies),
  [0025](./0025-gvisor-machine-posture-and-socket-narrowing.md) (narrowing what the socket
  grants), [0029](./0029-the-proxy-always-decrypts.md), [0032](./0032-a-root-collector-in-the-guest.md)
  (attribution foldyard owns; the same trust argument, one layer down).

## Context

The egress proxy is foldyard's one chokepoint: the allowlist, credential injection and the
network log all live there, on the host. It treats every client as a single principal:
*something in the VM*. When everything in the VM was the project's own stack, that was a fair
simplification ([0002](./0002-stack-colocation-project-as-isolation-unit.md)). It no longer is,
and the proxy has no way to tell the difference.

### What the proxy can't tell apart today

1. **Everything in the VM may reach it.** The VM firewall admits the walled principals (the VM
   user and its whole container subuid range: every container) to every host-side daemon port:
   `ip daddr $GW tcp dport { $PORTS } accept` (`assets/machine-wall/machine-wall.sh`). That
   covers every worktree's proxy and every token service.
2. **On a walled Lima VM, everything is pointed at it.** The walled VM's podman gives every
   container it creates the project's proxy URL (`config.wall_proxy_url`): the box, the stack's
   services, and whatever the box starts through the engine socket. That URL names the main
   checkout's port, so every worktree's stack runs on main's switches, whatever its own mode
   says. (On the podman backend there is no VM firewall, and the stack isn't pointed at the
   proxy at all.)
3. **Injection doesn't ask who.** A lone rule on a host writes its credential "whatever the
   request carries" (`egress_proxy._rule_for`). It also overwrites a credential the request
   brought itself, such as a service's own `x-api-key`. The dummy only chooses between rules
   that share a host. A client that skips TLS verification doesn't even need the proxy's CA.
4. **The client is gone by the time the request arrives.** A container's connection is re-made
   twice on the way: by pasta when it leaves the container network, and by Lima's user-mode
   network on the host. On a vz VM the proxy's peer is `limactl`'s hostagent, from a `127.0.0.1`
   port of its own. That was observed with `lsof` on 2026-10-22 and confirmed by the spike:
   once the connection leaves the rootless namespace, only that namespace's conntrack still
   holds the container's address. Nothing can join a proxy row to a container afterwards by
   address or port.
5. **Per-worktree proxies are a convention.** Each worktree's box is pointed at its own port
   ([0016](./0016-per-worktree-posture.md)), but by (1) any container can use any of them, and by
   (2) the stack's containers use main's.
6. **One principal is already told apart, and only partly:** a build started on the host, by a
   secret foldyard mints for it and puts in its proxy URL (`buildgate`). The secret decides what
   is tunnelled and which grants apply, but not credentials. On an injector host,
   `tls_clienthello` decrypts before it looks at the build, so a build is decrypted and injected
   there like the box. [networking.md](../networking.md) says builds are tunnelled, without that
   exception. This works at all only because foldyard creates the build. It doesn't create the
   containers the box starts.

### Why this matters beyond the guest monitor

- **Every container can spend the operator's credentials.** The token never enters the VM
  ([0007](./0007-credential-injection-at-egress-proxy.md); the GCP metadata emulator is the
  documented exception), but the power to spend it reaches every container in it.
  [security.md](../security.md) accepts this between worktrees ("a switch on in one worktree is
  usable from every box of that project") and gives the remedy: work you don't trust gets its own
  project. That advice fits work the *operator* starts. It doesn't fit work the *agent* starts as
  part of its task: a test container from an image it just pulled, an eval harness's sandboxes
  (BashArena runs each task's untrusted code in sibling containers the box creates), a
  `podman run` a README suggested. These can't be given their own project, because they *are* the
  project's work. Today each of them can send an HTTPS request to `api.anthropic.com` through the
  proxy and have the operator's subscription token written onto it.
- **Grants are one list, made blind.** A host granted for the box's `npm install` is open to an
  eval sandbox too. A refusal row can't say who asked: on 2026-10-21 the proxy refused
  `10.89.1.2:8083`, a container-network address, and nothing on the host could say which
  container had sent it. The Network Log offers the grant anyway.
- **The two records can't be joined.** The guest monitor knows which container made a
  connection, but every proxied connection has the same destination. In one replay batch, 936 of
  5,136 connect events read `192.168.5.2:42600` and nothing else. The proxy knows the destination
  but not the container. Learn mode's recommended `why` can name the tool that asked (its
  User-Agent), never the service.

## Decision

**Put a foldyard forwarder at the last point where a container still has its own address,
inside the VM's rootless network namespace, and make it the only way to the proxy. The forwarder
stamps each connection with where it came from. The proxy keys on the stamp, and the host
resolves the stamp to a container from the engine at read time.**

- **Where it sits.** Rootless podman's bridge networks share one network namespace in the VM.
  That covers every compose network: the box's, the stack's, the ones an eval harness creates. In
  it, each container has its own address (`10.89.x.y`). Everything leaving it is re-made by
  pasta, so this is the last place the client can be seen. The forwarder listens there, and the
  proxy URL the walled VM gives every container names the forwarder instead of the host gateway.
  The variable names and the protocol stay the same, so a tool that honoured the proxy keeps
  working, and a tool that ignored it was already refused by the firewall.
- **What it adds.** A proxy request's head is plain text even for HTTPS (`CONNECT host:443`, or
  absolute-form `GET http://…`). The forwarder reads the head and removes any stamp header the
  client wrote. It adds its own: the source address, its network, a per-boot sequence and an HMAC
  under a per-project key the host keeps (`<state dir>/forwarder.key`). After the head it splices
  bytes and parses nothing, including TLS. It passes `Proxy-Authorization` through untouched, so
  a host-started build keeps proving itself with its secret (spike finding 4).
- **It runs as root, provisioned at boot.** It follows 0032's pattern: a recorded boot script
  under its own marker, packaged code, never config. The key is written root-only, so the box
  can't read it through the socket. The forwarder listens *inside* the rootless namespace but
  makes its upstream connection from the VM's own namespace. The firewall's existing
  `meta skuid != {walled}` exemption already admits root, so closing the way round it is one
  deletion: the walled principals' daemon-port rule (finding 3).
- **It follows the namespace.** The rootless namespace is torn down with the last bridge
  container and re-created with the next one, and a listener can't survive that. The forwarder
  watches for the new namespace and listens in it. It holds the old socket until then, because
  namespace inodes are reused. The first container after a teardown would otherwise race the
  forwarder and fail closed (7 of 12 attempts succeeded at a 100 ms poll). So the walled VM's crun
  wrapper waits, bounded, for the forwarder's listener before a bridge container starts. With that
  wait, 12 of 12 succeeded (finding 2).
- **A second listener, in the VM's own namespace, is part of the design.** Some traffic never
  enters the bridge namespace. Rootless build `RUN` steps always run in the VM's namespace
  (rootless `podman build` can't join a network), and so do the VM's image pulls and
  `--network host` containers. The second listener attributes each connection by its owning
  process's cgroup:
  - a container's outermost `libpod-<id>.scope`, the walk 0032 already trusts;
  - a build step's `crun-buildah-*.scope`;
  - anything else is the VM user.
  Its firewall line is `ip daddr 127.0.0.1 tcp dport <listener> accept`: after the redirect, the
  hook still reports the outgoing interface, so `oifname "lo"` doesn't match. Lima's automatic
  port forwarding must be told to leave it alone, or it is forwarded onto your computer's
  loopback (finding 5).
- **The other network modes:**
  - **Rootless `podman run` defaults to pasta per container.** The walled VM makes bridge the
    default instead (`containers.conf`).
  - **An explicit `--network pasta` container can't be refused by the firewall** once builds are
    admitted, because pasta's helper runs in the podman CLI's scope. The second listener refuses
    it, or records it as the VM user's, by that same cgroup.
  - **gVisor (runsc):** a bridge container works and is stamped with its own address. With
    `--network host` it had no network at all. That fails closed, and the cause is not yet known
    (finding 9).
- **The proxy binds the stamp to the connection.** It verifies the stamp on a connection's first
  head. Every later request on that connection inherits it: a 407 retry (git relies on one), a
  keep-alive request, every request decrypted inside a CONNECT, and a passthrough tunnel's one row.
  The proxy strips any stamp header from those later requests, and from every request before it
  goes upstream, so a stamp never leaves. A connection without a valid stamp is recorded as
  `unattributed`. A dead forwarder means an immediate reset, never unstamped egress (finding 10).
- **Token services move behind it too.** Containers call some daemon ports directly (the GCP
  metadata emulator's token service, for one). Those are redirected and forwarded the same way,
  or narrowing the firewall cuts them off. The spike did not test this.
- **The host resolves at read time, over evidence.** The stamp names an address, and addresses
  are reused. With the guest monitor on, the host already asks the engine once per new container
  for its name and compose project (`monitorlog`'s claims). Resolution needs the same snapshots,
  with each endpoint's address, whether or not the monitor is on. A row takes the snapshot before
  it if that still holds its address, else the one after, never carried past a snapshot that
  dropped it (0032's rule for namespaces). The network is foldyard's own record for the networks
  it creates, so worktree attribution stops resting on labels the creator chose. Rows store the
  stamp. The Network Log, `fy monitor` and the hooks' batches resolve it, so a resolution fix
  applies to stored history.

### What it is for, in order

1. **Attribute and record, with no behaviour change.**
   - The Network Log shows who used each host, and a refusal names who asked.
   - `fy monitor` joins proxy rows to containers exactly.
   - Learn mode's recommendations name the service.
2. **Report.** `fy config widenings` lists the principals each credential was injected for, as
   observed. It is reported before anything is capped, as
   [0031](./0031-credentials-are-protocol-kinds-the-credential-owns-its-scope.md) did for scope.
3. **Scope.** Credentials go only to principals the adopted config names: by default, the box.
   An `unattributed` connection gets none. Grants can be scoped the same way. This ADR commits to
   that end state, and the attribution is built for it. Two facts shape the migration, which gets
   its own ADR because it changes behaviour:
   - **Where nothing is attributed, step 3 doesn't apply.** On the podman backend (one of the
     maintainers' consumers runs there), every connection is `unattributed`, its box included.
     There the proxy keeps today's behaviour and says so, rather than stripping the box.
   - **Stack services can rely on injection today.** One consumer's app service carries a dummy
     CMS token and depends on the proxy writing the real one, for draft preview, visual
     editing and the live content API. Its dummy is declared nowhere in foldyard config: it
     works only because a lone rule injects onto any request. Scoping needs a way for the
     adopted config to name such a service, and the change to a box-only default needs a
     changelog entry that names what breaks.

### What a stamp proves, and to whom

- **A container without the engine socket can't claim to be another container.** It can't forge
  the HMAC, can't reach the proxy around the forwarder, and can't change its own address. That
  needs `NET_ADMIN`, which podman's default capability set doesn't grant, and only a client of
  the socket can ask for it. In the spike, the forwarder stripped every client-written stamp it was
  sent, and the origin never saw one.
- **The box holds the socket, so it can act from inside any container.** It can `exec` into one,
  or start one on a free address. A stamp says *where* a request came from, not *who steered it*:
  0032 makes the same claim about names. What the box can't do is forge a stamp without being in
  that container's namespace. Entering one is an `exec`, which the guest monitor records when
  it's on. Narrowing the box itself belongs at the socket
  ([0025](./0025-gvisor-machine-posture-and-socket-narrowing.md)).
- **VM-root or a guest-kernel compromise can forge anything**
  ([0009](./0009-monitoring-cooperative-enforcement-locked.md)). The proxy, the store and the
  injected credentials stay on the host.

### What the spike settled

All figures are from [forwarder-spike.md](https://github.com/twistco/foldyard/blob/main/docs/forwarder-spike.md). The client ran in a bridge
container, and every sample was a new connection. mitmdump ran on the host with foldyard's
flags, fetching from a local origin. The two arms alternated each round, and each figure is the
median of round medians:

| | direct | via the forwarder |
| --- | --- | --- |
| small request, p50 | 2.05 ms | 1.99 ms |
| CONNECT + TLS, p50 | 9.10 ms | 9.04 ms |
| CONNECT + TLS, 8 parallel, p50 | 45.7 ms | 45.4 ms |
| throughput, 1 client | 331 MB/s | 342 MB/s |
| throughput, 4 parallel | 269 MB/s | 287 MB/s |

- **The worst difference seen** was +0.8 ms at p50 and +1.5 ms at p99. Throughput is capped by
  Lima's user-mode network at about 350 MB/s, with no proxy at all.
- **The forwarder replaces pasta's CPU rather than adding to it:** about 2.2 s per GiB, which pasta
  no longer spends on those bytes, plus 0.3–0.7 ms per new connection.
- **The Python prototype sustained 680–770 new connections a second**, about 40 times the load
  that stalled the proxy.
- **Not tested:** the real boot-script provisioning, token-service ports, `sock_diag`,
  inotify-based following, QEMU and x86_64, Linux and WSL2 hosts, podman 6, and load from real
  image builds. These are for the implementation to check, not reasons to doubt the design.

## Consequences

- **More root in the guest, and on the path.** 0032's collector observes beside traffic. The
  forwarder carries every connection, so a failure of the forwarder takes egress away. It is
  supervised, probed by the liveness check the proxy has, and loud. It never fails open.
- **Every VM re-provisions once.** The firewall change is in `machine-wall.sh`, whose every byte
  is in the provision id. Batch it with another change that needs the restart. Load the nft
  table atomically: in the spike, a restart piled up duplicate rules.
- **Lima only**, like the VM firewall, 0032 and
  [0033](./0033-a-to-box-and-a-from-box-beside-the-checkout.md). The podman backend stays
  unattributed and says so (step 3 above).
- **The stall that started this is most likely not the proxy's.** In the spike, Lima's user-mode
  network froze new connections for 7–69 s, mostly about 68 s, a SYN-retransmit schedule. It
  happened at the same rate with and without the forwarder. It also happened from the VM's own
  namespace with no container, pasta, forwarder or proxy involved: 16 of 4,000 connects, all
  workers at once. It never happened on the host itself. Neither design fixes it, and it needs
  its own investigation. The forwarder could retry its own upstream connect on a short timeout
  instead of the kernel's backoff (not tried).
- **The threat model changes** once step 3 lands. [security.md](../security.md)'s "the VM is the
  boundary" becomes: the VM bounds what code can reach, and the adopted config bounds who may
  spend which credential. Until then, the docs keep today's sentence.
- The egress log gains a field. The Network Log, the monitor join and learn mode's
  recommendations get better without a VM restart, because resolution runs on the host.

## Alternatives considered

- **A secret per container in its proxy URL, stamped by the runtime wrapper.** This is the
  fallback the spike measured. The walled VM already runs every create through a crun wrapper
  that edits the spec (`91-fy-proxy-ca-runtime.conf`). The wrapper gets the container id as its
  last argument and sees the final merged environment, so it could set each container's proxy URL
  to `http://<id>:<hmac(key, id)>@…`. It could even rewrite a proxy URL set by compose or a
  Dockerfile. The proxy would recompute the HMAC.
  - *For it:* identity is the engine's own container id; every network mode works alike; there is
    no new daemon and no address reuse.
  - *Against it, measured:*
    - **The credential is a bearer secret in each container's environment**, which
      `podman inspect` shows to anything holding the socket and which can be handed to another
      container. The wrapper runs as the VM user, so the key is readable through the socket too,
      unless a root signer is added. Under the forwarder, the box has to enter a container to
      speak as it, where the monitor sees it.
    - **Clients:** curl, busybox wget, pip, Python's urllib, uv, npm, Node's fetch with
      `NODE_USE_ENV_PROXY=1`, Go and apt send the credential from the proxy URL. git sends it
      only after a 407 challenge. The JVM ignores `HTTPS_PROXY`, and with system properties it
      still sends no credential. A proxy set in a tool's own config file bypasses the wrapper.
  - **Not preferred.** It is cheaper to build, but its main weakness, a bearer secret anyone with
    the socket can read, is fundamental. None of the forwarder's caveats is. It remains the fallback
    if implementing the namespace following turns out more fragile on other hosts than on the
    spike's.
- **Join by port.** The proxy's client port can't be matched to the kernel's connect event:
  pasta and Lima's user-mode network each re-make the connection (Context, 4).
- **Join by time.** This is fine for analysis, but ambiguous with parallel workloads, so it isn't
  evidence.
- **Capture the request line in the kernel** (a Tetragon probe on the first send to the proxy
  port). It gives the container and the destination in one event, with no new component. But a
  client can split the line across writes and the proxy still puts it back together, so it is
  evadable by exactly the clients that matter. It is observation only, so it can't scope a
  credential.
- **Lima's `vzNAT`**, which keeps the source port all the way to the host. It fixes only the
  host hop: bridge containers still share one pasta. It would also make the proxy listen beyond
  loopback.
- **A project per untrusted workload**, today's advice. It is right for work the operator starts.
  An agent can't make a project mid-task, and an eval harness starts dozens of sandboxes a run.
