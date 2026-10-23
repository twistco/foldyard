# Lima user-mode network freezes: investigation (2026-10-23)

Finding 8 of the forwarder spike behind ADR-0034 (`docs/forwarder-spike.md`, on the
`feat/attributed-egress` branch until it merges) reported this: new guest→host connections freeze for 7–69 s (mostly ~68 s), in bursts, with
every worker frozen at once, on Lima's default `vz` network. It happens with no container, pasta,
forwarder or proxy in the path. This page is the record of a follow-up run that set out to find
the component and the mechanism, measure where it bites, compare the alternatives, and rank what
foldyard could do about it. It also asks whether this explains the 2026-10-22 stall: about six
minutes of `connection timed out` to the proxy during four image builds.

**Short answer: the mechanism is proven.** It is a TIME_WAIT bug in gVisor's netstack, which
Lima's hostagent runs through gvisor-tap-vsock. It is **not** the forwarder's `maxInFlight = 10`
cap. That cap is a real, separate freeze, and it is also reproduced below.

- **The cause, step by step:**
  1. The host side closes first, so the gVisor endpoint ends in TIME_WAIT, which lasts 60 s.
  2. The guest closed second, so it holds no TIME_WAIT of its own. Its port is free at once.
  3. The guest's kernel soon reuses that source port for the same destination.
  4. gVisor silently drops the reopening SYN, because the stack accepts through a `tcp.Forwarder`,
     not a listening endpoint. It drops every retransmission too, until its TIME_WAIT expires.
- **How long a freeze lasts:** the time left on gVisor's 60 s timer, rounded up to the guest's next
  SYN retransmission (…, 19, 35, 67 s). That is why the stalls cluster at ~68 s and ~36 s.
- **Upstream:** google/gvisor#15013 describes this bug, and its fix, PR #15019, is open and
  unreviewed. No Lima or gvisor-tap-vsock release carries a fix, and Lima master doesn't either.
- **The fix works (Finding 12):** Lima 2.1.3 rebuilt with #15019 backported had **0 stalls in
  35,000 connections**, where the same build without it stalled ~68 s on every reuse. A 1 s gVisor
  TIME_WAIT, the quick workaround, only shortens each stall to ~1 s.
- **Reported:** a measured comment on
  [google/gvisor#15019](https://github.com/google/gvisor/pull/15019#issuecomment-6362902795), and
  [containers/gvisor-tap-vsock#757](https://github.com/containers/gvisor-tap-vsock/issues/757) for
  a configurable TIME_WAIT.
- **foldyard's workaround:** the ADR-0034 forwarder (#130) closes its connection to the proxy with an
  RST and re-dials a slow connect (Mitigations 1–2). That only covers VMs with attribution turned
  on.

Nothing here is product code. The scripts are in [lima-freeze-kit/](./lima-freeze-kit/). The raw
logs (48 MB of per-connection JSON) aren't kept; a log named below (`pr-def-47324`, …) is the run
that produced it.

## Where it ran

| | |
| --- | --- |
| Host | macOS 27.0.1 (26A434), Apple M3 Max (16 cores). Host TCP: `kern.ipc.somaxconn` 128, `net.inet.tcp.keepinit` 75000 ms, `msl` 15000 ms, ephemeral range 49152–65535 |
| Lima | 2.1.3 (Homebrew; `brew info` offers stable 2.2.1). Vendored `github.com/containers/gvisor-tap-vsock v0.8.9` and `gvisor.dev/gvisor v0.0.0-20240916094835-a174eb65023f` (lima `go.mod`) |
| VM (vz) | `fy-spike-fwd`, the throwaway from yesterday's spike: `template:podman`, 4 vCPU / 4 GiB, `vmType: vz`, the default user-mode network (guest `192.168.5.15`, gateway `192.168.5.2`), no foldyard provisioning, no wall |
| VM (qemu) | `fy-freeze-qemu`, made for this run and deleted afterwards: the same Fedora 44 Cloud image, `vmType: qemu` (QEMU 11.1.1, HVF), 4 vCPU / 4 GiB, nothing mounted. Lima's qemu network is QEMU's own slirp: `-netdev user,id=net0,net=192.168.5.0/24,…` (read from the running process) |
| Guest | Fedora 44, kernel `6.19.10-300.fc44.aarch64`, Python 3.14.3, podman 5.8 + pasta `0^20260120`. TCP defaults: `tcp_syn_retries = 6`, `tcp_syn_linear_timeouts = 4`, `ip_local_port_range = 32768 60999`, `tcp_tw_reuse = 2` |
| Origins | `lima-freeze-kit/origin.py`: a host-loopback asyncio server, one or more ports in 47300–47329. It reads a request head, answers 100 bytes and **closes first**, as the spike's origin did, and as a proxy does on `Connection: close`. It logs every accept with a timestamp |
| Instruments | Guest: `tcpdump -i eth0`, plus per-connection JSON from the client (start, connect time, local port). Host: `lima-freeze-kit/hostpoll.sh` every 0.5 s, recording **gVisor's own TCP counters** from the instance's gvisor-tap-vsock HTTP endpoint (`curl --unix-socket ~/.lima/<inst>/default_ep.sock http://lima/stats`) and the hostagent's TCP sockets by state (`lsof -p <ha.pid>`) |

Nothing outside `fy-spike-fwd` and `fy-freeze-qemu` was touched, apart from `limactl list` and
reading the affected VM's `ha.stderr.log` / `ha.stdout.log`. That includes not querying any other
instance's `/stats`. (`foldyard-example` was Running when this began and Stopped by the end. This
run didn't do that.) Every guest sysctl changed on `fy-spike-fwd` was put back to its default.

## Findings

### 1. Reproduced at once, and gVisor's in-flight cap is not involved

```sh
limactl shell fy-spike-fwd -- python3 /tmp/fz/client.py 192.168.5.2 47300 500 8 --out run1.jsonl
```

After 2,592 connections (0.36 s), all 8 workers froze together for **68.22 s**. The other 3,992
connects took 0.39 ms at p50.

Over the same window, the host side showed:

- **gVisor's `ForwardMaxInFlightDrop` stayed at 0.** That counter is the only one that moves when
  the forwarder drops a SYN for being over its cap. So the `maxInFlight` hypothesis is out for
  this stall.
- **gVisor's `CurrentConnected` sat at 2,597 for exactly 60 s, then fell.** That is every
  connection of the run, held in TIME_WAIT. It went 2,597 → 954 → 0 at +60 s, and the stall
  ended at +68 s.
- **The origin logged no accept on that port for 68.22 s.**
- **The hostagent held no `SYN_SENT` socket during the stall:** its sockets were
  `{ESTABLISHED: 1, LISTEN: 2}` throughout. The SYN never got as far as a host-side dial.

### 2. The component: gvisor-tap-vsock's netstack, inside the hostagent

On `vz`, with no `networks:` usernet entry, Lima starts an in-process gvisor-tap-vsock:

- **The stack itself.** `pkg/driver/vz/vm_darwin.go:178-210` (`startUsernet`, "Start a in-process
  gvisor-tap-vsock") calls `usernet.StartGVisorNetstack`. That builds a gvisor-tap-vsock
  `types.Configuration` with `NAT: {gatewayIP: "127.0.0.1"}` (`pkg/networks/usernet/gvproxy.go:70-84`).
- **The guest link.** It is a `VZFileHandleNetworkDeviceAttachment` on a unix datagram socketpair
  (`vm_darwin.go:417-432`, `921-940`).
- **The guest's gateway.** `192.168.5.2` is gVisor's netstack inside `limactl hostagent`, and the
  guest's TCP connections to it are terminated there.
- **The relay.** gvisor-tap-vsock's TCP forwarder (`pkg/services/forwarder/tcp.go`, v0.8.9) makes
  a new host-side `net.Dial` to `127.0.0.1:<port>` for each one and copies bytes between the two.
- **The stats endpoint.** The same process serves gvisor-tap-vsock's HTTP mux on
  `<inst>/default_ep.sock` (`vm_darwin.go:184`, `gvproxy.go:103-108`). `/stats` there dumps
  gVisor's `tcpip.Stats`, which is what made the counters above readable.

The alternatives, checked against the source and the upstream research summarised under
Finding 9:

- **`networks: [{lima: user-v2}]`** runs the same code in a separate `limactl usernet` process,
  shared by every instance on that network.
- **`vmType: qemu`** uses QEMU's built-in slirp. Confirmed on the running process.
- **`vzNAT: true`** adds a second NIC on Apple's vmnet NAT. eth0 stays on gvisor-tap-vsock.

### 3. The mechanism: a SYN that reopens a gVisor TIME_WAIT is dropped, and nothing logs it

The guest capture of the first stalled worker (`run1.pcap`, port 43930) shows the whole story:

```
370.902129  .15.43930 > .2.47300  [S]  seq 1743298327           first incarnation
370.902780  .2.47300 > .15.43930  [S.]
…            request, response
370.903579  .2.47300 > .15.43930  [F.]                           gVisor closes FIRST (host side closed)
370.903736  .15.43930 > .2.47300  [F.]                           guest closes second: no TIME_WAIT on the guest
370.903824  .2.47300 > .15.43930  [.]   ack 53                   gVisor -> TIME_WAIT (60 s)
371.255844  .15.43930 > .2.47300  [S]  seq 1748825161           SAME 4-tuple, 0.354 s later, higher seq
372.277846 … 376.375256  [S] ×5 (+1, +2, +3, +4, +5 s)           no answer
378.422463, 382.455478, 390.838118, 407.226719  [S]  (+7, +11, +19.6, +36 s)   no answer
439.478136  .15.43930 > .2.47300  [S]                            +68.2 s
439.478883  .2.47300 > .15.43930  [S.]                           answered: TIME_WAIT has expired
```

All 8 stalled connections were the same case: a port reused 0.352–0.355 s after its previous
incarnation. The new SYN's sequence number was above the old connection's: 1748825161 against
1743298327 + 53. Its timestamp was newer too. RFC 1122 lets a stack reopen a TIME_WAIT in that
case.

**The source** (gVisor `a174eb65023f`, as vendored by Lima 2.1.3):

- **`rcv.go:552-616` `handleTimeWaitSegment`.** A SYN with `RcvNxt.LessThan(segSeq)` returns
  `newSyn = true`, meaning "try and redirect it to a listening endpoint if available". An RST in
  TIME_WAIT is "silently drop[ped]" (RFC 1337), so the guest cannot clear it either.
- **`connect.go:1417-1460` `handleTimeWaitSegments`.** On `newSyn`, it looks up an endpoint for the
  local address and port, and redirects only `if EndpointState(tcpEP.State()) == StateListen`.
  Otherwise the segment is released and nothing is sent.
- **Why that lookup finds nothing here.** gvisor-tap-vsock never listens. It accepts everything
  through `tcp.NewForwarder(s, 0, 10, …)` installed as the protocol's default handler, so there is
  no listening endpoint to find.
- **Nothing extends the timer.** The dropped SYN doesn't extend TIME_WAIT either (`extTW` stays
  false), so the endpoint lives exactly `DefaultTCPTimeWaitTimeout = 60 * time.Second`
  (`protocol.go:65`). gvisor-tap-vsock and Lima never set `TCPTimeWaitTimeoutOption`: there are no
  hits for `TimeWait` in either tree.
- **Nothing logs it.** Nothing on this path logs, and no counter names it.

**The duration rule.** A frozen connect lasts the TIME_WAIT time left on the old incarnation,
rounded up to the guest's next SYN retransmission. Linux 6.19 with `tcp_syn_linear_timeouts = 4`
sends SYNs at 0, 1, 2, 3, 4, 5, 7, 11, 19, 35 and 67 s, and gives up at 131 s
(`Documentation/networking/ip-sysctl.rst`). Every stall in one default-sysctl run
(run `pr-def-47324`) fits the rule:

| previous use of the 4-tuple | TIME_WAIT left | next retransmission | measured stall |
| --- | --- | --- | --- |
| 53.1 s before | 6.9 s | 7 s | 7.2 s |
| 48.9 s | 11.1 s | 11 s (+jitter) | 11.2 s |
| 45.5 / 42.9 / 41.6 s | 14.5 / 17.1 / 18.4 s | 19 s | 19.4 / 19.4 / 19.6 s |
| 30.9 / 26.8 / 24.4 s | 29.1 / 33.2 / 35.6 s | 35 s (+jitter) | 35.7 / 35.9 / 36.0 s |
| 12.0 / 11.1 / 10.7 s | 48–49 s | 67 s | 68.3 / 68.0 / 68.1 s |

Across every `vz` run with default SYN sysctls (142,200 connections), connects over 0.9 s
clustered only at retransmission points:

| ~1 s | 2 s | 5 s | 7 s | 11 s | 19–20 s | 36 s | 68 s |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 14 | 2 | 3 | 5 | 3 | 7 | 15 | 94 |

The 68 s mode is the common case: a port reused within ~25 s of its last use waits for the
retransmission at 67 s.

**Upstream has the same diagnosis:**

- **gVisor.** [google/gvisor#15013](https://github.com/google/gvisor/issues/15013) is open: "SYN in
  TIME_WAIT is dropped when the stack accepts through `tcp.Forwarder`". The fix,
  [PR #15019](https://github.com/google/gvisor/pull/15019) ("tcp: forward SYN in TIME_WAIT to the
  protocol handler"), is open and unreviewed, last active 2026-09-26.
- **Coder** hit it independently ("the client's dial hung for up to about 63s") and pins a
  cherry-pick ([coder/coder#30471](https://github.com/coder/coder/pull/30471)).
- **Neither** project's issue mentions Lima or gvisor-tap-vsock.

### 4. Why "all workers at once": Linux's per-destination port walk

The guest closes second, so it holds no TIME_WAIT of its own. The kernel may therefore reuse a
port as soon as the previous connection is gone. The kernel picks ports by RFC 6056 algorithm 4
(`net/ipv4/inet_hashtables.c`, `__inet_hash_connect`):

- **One walk per destination.** Each (source, destination, destination port) has its own walk
  through the range.
- **Small steps.** The walk moves 2–16 ports per connect (`table_perturb[index] += i + 2`, with
  `i = max(i, rand(8) * 2)`). The measured step was uniform over 2–16, median 10.
- **A new start every 10 s.** `secure_ipv4_port_ephemeral` hashes in
  `jiffies / EPHEMERAL_PORT_SHUFFLE_PERIOD` (`net/core/secure_seq.c:25,110`, `10 * HZ`), so every
  10 s each destination's walk starts again from a new random place. The client logs show the jump
  at the same 10 s boundaries (9.4, 19.4, 29.4 s…) in five parallel clients.

All workers aimed at one host port share one walk. A collision happens once the walk re-enters
ports used in the last minute, either because it wrapped or because a 10 s reshuffle landed it
there. From then on, each next connect has a good chance of landing on a port still in gVisor's
TIME_WAIT. The workers that hit one stop, and within a few milliseconds that is all of them. In
run 1 the eight froze between 0.354 and 0.360 s.

The freeze is **per 4-tuple, not global**. The evidence:

- **Other destinations kept working.** In the five-client run below, each client stalled on its own
  schedule while the others kept connecting. A different host port answered normally throughout.
- **Established connections were unaffected.** A canary sending 16 bytes every 50 ms over one
  established connection through 200 s of stalls logged 3,851 round trips, worst 15.7 ms
  (run `e1-canary`).

### 5. Thresholds: there is no safe rate

Each row is one worker connecting to its own host port, host closing first, for 15 min (2–10/s)
or about 4–7 min (25–110/s). The 25–110/s rows ran as five parallel clients on five ports, and
the 2/5/10/s rows as three. The rate limiter catches up after a stall in a burst, so each row
slightly overstates what a steady client would see.

| target conn/s | connections | wall s | connects > 1 s | stalls ≥ 30 s | share of wall time spent frozen | first stall ≥ 30 s at |
| --- | --- | --- | --- | --- | --- | --- |
| 2 | 1,800 | 900 | 5 | 4 | 31% | 247 s |
| 5 | 4,500 | 918 | 12 | 6 | 41% | 333 s |
| 10 | 9,000 | 900 | 18 | 11 | 74% | 36 s |
| 25 | 4,500 | 248 | 3 | 3 | 82% | 19 s |
| 40 | 7,200 | 228 | 3 | 3 | 90% | 21 s |
| 55 | 9,900 | 306 | 4 | 4 | 89% | 31 s |
| 75 | 13,500 | 297 | 4 | 4 | 92% | 19 s |
| 110 | 19,800 | 433 | 14 | 6 | 93% | 26 s |

**Concurrency is not the trigger:** one sequential worker at **2 connections a second** stalled
for ~68 s four times in 15 minutes. What matters is the number of recent host-closed connections
to the same destination, since they decide how dense gVisor's TIME_WAIT set is along the guest's
walk. Concurrency only decides how many callers are caught at once.

Unthrottled runs reach a ceiling. Four workers × 5,000 settled at **48 connections a second**,
with the workers frozen about 90% of the time. That ceiling is gVisor's 60 s TIME_WAIT against a
~28 k port range.

Spreading over more destination ports spreads the TIME_WAIT set, because each port has its own
walk and its own 4-tuples. It doesn't help foldyard: the wall sends all box egress to one proxy
port per worktree.

### 6. It only happens when the host side closes first

Each arm ran 4 workers × 5,000, unthrottled, each on a fresh port:

| arm | conn/s | connects > 1 s | max connect | note |
| --- | --- | --- | --- | --- |
| baseline (host closes first) | 48.5 | 22 | 68.4 s | wall 412 s for 20,000 |
| guest closes first (`--nodata`: connect, close) | 4,671 | 0 | 68 ms | but 3,757 `ECONNREFUSED`: at this rate the hostagent's own host-side ephemeral ports (16,384, each in TIME_WAIT for 30 s on the host) run out and its `net.Dial` fails. That is a separate limit, at ~550/s sustained, far above real traffic |
| **guest aborts** (`--abort`: read to EOF, then `SO_LINGER {1,0}` → RST instead of FIN) | **7,398** | **0** | **37 ms** | gVisor gets the RST before its own FIN is acknowledged, so it never enters TIME_WAIT |

When the guest closes first it is the guest that holds TIME_WAIT, which also keeps it from reusing
the port. Either way, the bug needs **host-first closes**: the origin closing after
`Connection: close`, or the proxy closing a client after an upstream close, an error, a refusal or
an idle timeout. How often foldyard's real proxy traffic closes host-first was not measured.

### 7. Containers hit it the same way through pasta

A `python:3.13-alpine` container on the bridge network ran 4 workers × 1,500 to the host: frozen
**68.3 s, all four at once, 0.6 s in** (run `ctr2`). Pasta re-makes the connection in the
VM's own namespace, and the capture there shows the source port chosen by the VM's kernel, not the
container's: container ports 53976… against VM ports 42494…. The host side closes first. So the
VM's sysctls and port walk, not the container's, govern the SYN to the gateway. The same holds for
an ADR-0034 forwarder's upstream connection. (An earlier container run made 6,000 connections with
no stall, but it ran under the widened port range and finished inside two 10 s epochs, so it
doesn't count against this.)

### 8. The other freeze: `maxInFlight = 10` (real, fixed upstream, not this stall)

The hypothesis in the brief describes a real, separate failure, and it was reproduced. A forwarder
slot is held from the SYN until `Complete()`, which means through the blocking `net.Dial`
(v0.8.9: no timeout) and the guest-side handshake (`forwarder.go:84-92`, `accept.go:305-318`).

The test: 12 guest connects to an unroutable address (`192.0.2.1:80`), while a probe dialled a
healthy host port and `1.1.1.1:443` once a round.

- **The hostagent's `SYN_SENT` count was pinned at exactly 10 for 75 s,** which is macOS's
  `net.inet.tcp.keepinit`. Ten of the 12 flood connects got `ECONNREFUSED` at 75 s. Two never got
  a slot and timed out at 95 s.
- **Every new TCP connection the guest made failed for ~74 s,** to the host and to the internet
  alike. `ForwardMaxInFlightDrop` went from 0 to 86. DNS (UDP) wasn't tested.
- **Nothing logs it,** except `net.Dial()` failures at Trace level.

This is [containers/gvisor-tap-vsock#676](https://github.com/containers/gvisor-tap-vsock/issues/676),
fixed by [PR #698](https://github.com/containers/gvisor-tap-vsock/pull/698): cap 128, a 30 s dial
timeout. The fix is in gvisor-tap-vsock v0.9.0 and Lima master
([lima#5616](https://github.com/lima-vm/lima/pull/5616), milestone v2.3.0, due 2026-10-30). Lima
2.2.1 and 2.3.0-beta.1 still pin v0.8.9.

**On a foldyard VM it needs a host-side dial that hangs.**

- **The wall rules out the internet.** foldyard's wall (`src/foldyard/assets/machine-wall/machine-wall.sh:394-403`)
  lets guest TCP reach the gateway only on foldyard's daemon ports, and on `:53` for private
  ranges, and rejects the rest in the guest with a TCP reset. A blackholed internet address never
  reaches gVisor.
- **A host listener that stops accepting doesn't pin a slot either.** The proxy origin was stopped
  with `SIGSTOP` while 300 connects arrived: 128 queued, and the rest were **refused at once**
  (138 `ECONNREFUSED`, 34 `ECONNRESET`), never hung. This ran on the QEMU VM, whose slirp dials
  through the same host kernel. It was not repeated on vz.
- **What a frozen proxy looks like instead.** A frozen proxy gives the box `connection refused`
  (gVisor answers a failed dial with RST) or a read timeout, **not** a connect timeout.

### 9. Alternatives

- **`vmType: qemu` (QEMU slirp): no freeze.** Same image, same kernel, same host origin, host
  closing first:

  | run | result |
  | --- | --- |
  | 8 × 500 | 4,000 in 0.6 s, max 2 ms |
  | 4 × 20,000 | 80,000 at 1,800/s, max 52 ms |
  | 8 × 5,000 | 40,000, two connects at exactly 1.0 s |
  | 110/s for 120 s | 13,200, max 37 ms |

  133,200 connections in all, none over 1.0 s. libslirp makes a host socket per guest connection
  and has no 60 s TIME_WAIT trap of this kind. Not measured: bulk throughput, CPU and file-sharing
  cost against vz, and foldyard's own provisioning on qemu. (ADR-0033 allows the to-box on
  qemu + 9p.)
- **`networks: [{lima: user-v2}]`:** the same gvisor-tap-vsock code in a shared `limactl usernet`
  process (`cmd/limactl/usernet.go:88-95`). It has the same bug, and the TIME_WAIT set and the
  in-flight cap become shared between VMs. Not run.
- **`vzNAT: true`:** **not measured**, because this run kept to loopback listeners on
  your computer, and the vzNAT gateway (`192.168.64.1` on `bridge100`, by Lima's docs) reaches
  only host services bound beyond loopback (lima#5581). gVisor isn't on that path at all: the
  guest talks to the host's own TCP stack through Apple's NAT, so this bug can't apply by
  construction. What using it for the proxy would take:
  - the proxy listening on the bridge address (or `*`) instead of `127.0.0.1`;
  - every vzNAT VM on your computer sharing that subnet and being able to reach it;
  - the address not being configurable, and existing only while a vzNAT VM runs;
  - the guest wall allowing that address and rejecting eth1's direct internet route;
  - macOS's application firewall possibly prompting.

  Today every Lima VM can already reach every loopback listener through its own `192.168.5.2`. So
  cross-VM reachability of the proxy isn't new, but it would no longer be per-VM NAT.
- **A newer Lima:**

  | Lima | gvisor-tap-vsock | in-flight cap | TIME_WAIT bug |
  | --- | --- | --- | --- |
  | 2.2.1 (Homebrew stable) | v0.8.9 | unfixed | unfixed |
  | 2.3.0-beta.1 | v0.8.9 | unfixed | unfixed |
  | master, released as 2.3.0 | v0.9.0 | fixed | unfixed: gVisor `20260413` lacks #15019 |

### 10. Guest-side settings, measured

Each run was 4 workers × 1,500, unthrottled, on a fresh port, so stalls are near-certain. Every
setting was restored afterwards.

| setting | measured effect |
| --- | --- |
| default (`syn_retries 6`, `linear 4`) | 68.2–68.4 s |
| `tcp_syn_linear_timeouts=70`, `tcp_syn_retries=70` (a SYN every second for 70 s) | **60.41 s**: the stall ends within ~0.4 s of the TIME_WAIT expiring. Only 11% off the worst case |
| `tcp_syn_retries=3` | the kernel gives up at **19.3 s** with `ETIMEDOUT` ("Connection timed out", errno 110). A client that retries on that gets a new port and usually succeeds: 10 at 19 s, 4 at 20–22 s, 1 at 39 s |
| `tcp_syn_retries=1` | gives up at **3.07 s**. With a retrying client: 112 at 3.1 s, 13 at 6.1 s, max 15.4 s |
| `ip_local_port_range=1024 65535` (A/B: 3 clients × 10/s × 600 s per arm) | wide: 19 connects > 1 s, 16 stalls ≥ 30 s. Default: 25 and 16. **No measurable benefit** |
| client connect timeout 2 s + re-dial (`--retry 2`) | max **10.0 s** instead of 68 s (549 at 2 s, 103 at 4 s, 21 longer). Saturated throughput unchanged at 47/s: retries keep landing in the same dense stretch of the walk |

In a walled foldyard VM, every guest SYN goes to `192.168.5.2`. That is a virtual link, where an
unanswered SYN means one of these two bugs, not congestion. So a short guest SYN budget costs
little, but it turns a 68 s hang into an error for any client that doesn't retry.

### 11. The 2026-10-22 logs

The affected VM's `ha.stderr.log`, 21:10–21:30 local:

- **The hostagent was alive throughout.** It logged `Time sync` every 10 s, with no gap over 12 s.
- **Nothing else was logged** except four NTP `Stopping udp proxy (read udp …:123: i/o timeout)`
  idle-timeout lines, which are routine.
- **No forwarder errors.** There is no `r.CreateEndpoint()` line and no warning or error.

`ha.stdout.log` has nothing in the window. That absence proves nothing either way: by
construction, neither freeze logs anything at the hostagent's default (debug) level. A TIME_WAIT
drop is never logged, an in-flight drop is only a counter, and a dial failure is Trace.

### 12. The upstream fix, measured downstream

Lima v2.1.3 was built from source three ways, all ad-hoc signed with the vz entitlement:
- **control:** unchanged;
- **#15019:** both of the PR's commits (head `e503a89`) backported onto the vendored gVisor
  `a174eb65023f`. The 36-line non-test diff applies with `patch -p1` at line offsets only, wired in
  with a local `replace gvisor.dev/gvisor`. #15013's reproducer fails on stock `a174eb65023f` and
  passes with the backport under `-race`;
- **1 s TIME_WAIT:** gvisor-tap-vsock's `createStack` setting `TCPTimeWaitTimeoutOption` to 1 s.

Each started the same `fy-spike-fwd` VM; the hostagent was checked to be running from the binary
that started it. Guest sysctls were at defaults, and the host closed first.

| guest→host | control | #15019 | 1 s TIME_WAIT |
| --- | --- | --- | --- |
| 8 workers × 500, three runs | 8 / 10 / 8 stalls of ~68 s | 0 over 1 s, max 7 ms | 8 per run, each 1.04 s |
| 4 × 5,000, unthrottled | 48.6 conn/s, 24 stalls of ~68 s, 412 s | 8,032 conn/s, 0 over 1 s, max 10 ms, 2.5 s | 2,171 conn/s, 24 stalls of ~1.05 s |
| 1 worker at 10/s, 5 min | 12 stalls (three of ~68 s) | 0 over 1 s, max 15 ms | 0 over 1 s, max 24 ms |
| SYNs reopening a 4-tuple still in gVisor's TIME_WAIT | 62, every one stalled | 9,506, every one answered | — |

- **The control reproduces the baseline:** 48.6 conn/s, 24 stalls and 412 s, against Finding 6's
  48.5, 22 and 412.
- **`ForwardMaxInFlightDrop` stayed 0** in every run, so the in-flight cap played no part.
- **#15019 keeps TIME_WAIT and changes only the reopen.** `CurrentConnected` still reached 22,784,
  and `ResetsSent` didn't move.
- **A short TIME_WAIT shortens, it doesn't remove.** Each stall becomes the wait for the guest's
  first SYN retransmission.

## Verdict

**The finding-8 stall: proven.** The evidence:

- **The capture** shows the reused 4-tuple, the unanswered SYNs and the late SYN-ACK.
- **The counters** show the TIME_WAIT set held for 60 s, and in-flight drops at zero.
- **The host side** saw nothing: no accept, no dial.
- **The duration rule** predicts every measured stall.
- **The source** shows exactly where the SYN is dropped.
- **Upstream** has an open issue with the same diagnosis.
- **Its fix, backported,** removes every stall (Finding 12).

It is gVisor's TIME_WAIT handling under gvisor-tap-vsock's `tcp.Forwarder`, inside Lima's
hostagent. It hits any steady stream of host-closed connections to one destination, at rates as
low as 2 a second. It reaches containers through pasta, and an in-VM forwarder's upstream, in the
same way. A Lima upgrade doesn't fix it, nor does any released gvisor-tap-vsock. QEMU's slirp is
unaffected.

**The 2026-10-22 stall: likely contributes, not proven, and probably not the whole story.**

What fits:

- **The error itself.** `connection timed out` to the proxy is a connect-phase failure, so the
  SYNs went unanswered.
- **Only two things on this path do that.** The TIME_WAIT bug and the in-flight cap. A frozen proxy
  gives `refused` or read timeouts instead (Finding 8).
- **Burst shape and stall length.** Bursts that catch every concurrent client of one proxy port at
  once, with stalls of up to 68 s, fit four parallel builds. So does a client whose own connect
  timeout is under 68 s reporting `connection timed out`.

What doesn't fit:

- **The length of the gap.** **About six minutes with zero proxy rows** is longer than this
  mechanism produced here. Even saturated, the longest window per destination with no completed
  connection was 68.4 s (`e3-base`, `e1-*`). In the 4-worker unthrottled run, workers were frozen
  90% of the time and still completed connections between freezes. With a 2 s retry the longest
  gap was 2.0 s.

The in-flight cap would freeze everything for 75 s per pinned slot, and could chain. But it needs
host dials that hang, and the wall keeps blackholes in the guest. That holds if the affected VM was
walled, which wasn't checked read-only.

**Three explanations remain.** A third cause, the proxy's log not recording what it did, or the
TIME_WAIT bug plus clients that back off for minutes. The logs can't separate them, because both
freezes are silent. The operator's call on how to settle it is under Mitigations, item 6.

## Mitigations, ranked

1. **Abortive close in a guest hop foldyard owns.**
   - **What:** if the ADR-0034 forwarder is built, its upstream socket should close with
     `SO_LINGER {1,0}` once both directions are finished. In practice, after reading the
     upstream's EOF and flushing what it owes upstream: check `SIOCOUTQ` is 0, or the RST discards
     unacknowledged bytes.
   - **Measured:** 20,000 connections at 7,398/s, **0** stalls, max 37 ms (Finding 6). gVisor then
     never holds TIME_WAIT for proxy connections, whatever the client in the container does.
   - **Cost:** the forwarder itself. Without one, no foldyard component closes the gateway-side
     socket: pasta does, for containers.
2. **A short connect timeout and re-dial in that same hop.**
   - **Measured:** 2 s and re-dial bounded a connect at 10 s, mostly 2 s (Finding 10).
   - **Use:** with item 1, a belt and braces measure. It also covers a SYN at or below `RcvNxt`,
     which even gVisor #15019 leaves to wait out TIME_WAIT. It costs nothing on a healthy path:
     p50 connect 0.2 ms.
3. **Guest `net.ipv4.tcp_syn_retries` = 2 or 3, VM-wide.**
   - **Measured:** 3 gives up at 19.3 s, 1 at 3.1 s (2 not measured: expect ~7 s). With a retrying
     client, the delay drops to 3–20 s.
   - **Reach:** it covers every path, because pasta's and any forwarder's SYNs are made in the VM's
     namespace (Finding 7). It is cheap.
   - **Costs:**
     - Hangs become errors for clients that don't retry. pip, uv, npm, cargo and go mostly retry.
       curl doesn't by default.
     - It belongs in `guest-boot.sh`, whose every byte is the provisioning id. So it refuses every
       existing VM until `fy machine stop && fy up`. Batch it with another change.
4. **Upstream.**
   - **gVisor:** [google/gvisor#15019](https://github.com/google/gvisor/pull/15019) is the fix,
     measured downstream in Finding 12; the result is posted on the PR.
   - **gvisor-tap-vsock:** [#757](https://github.com/containers/gvisor-tap-vsock/issues/757) asks for
     a short `tcpip.TCPTimeWaitTimeoutOption`, configurable beside v0.9.0's `TCPMaxInFlight`. That is
     the one-line workaround #15013 suggests. The guest holds the real TIME_WAIT here, so a 1–2 s
     timer on gVisor's side protects very little, but it only shortens each stall to the guest's
     first SYN retransmission (~1 s, Finding 12). #15019 removes it.
   - **Lima:** then a Lima release with either, or Lima carrying #15019 with a `replace` until it
     merges. A Lima issue links the two.
   - **Lima 2.3.0 when released** fixes the in-flight freeze (Finding 8). That matters if any host
     dial behind the gateway can hang.
5. **A different network path, a bigger change for the operator.**
   - **`vmType: qemu`:** 0 freezes in 133,200 connections. Its other costs are unmeasured.
   - **vzNAT for the proxy:** gVisor isn't on that path, but the proxy would have to listen off
     loopback (Finding 9). Not measured.
6. **Measure the next occurrence, before acting on the incident.**
   - **The counters:** gVisor's counters are one read-only HTTP GET on the live hostagent:
     `curl --unix-socket ~/.lima/<inst>/default_ep.sock http://lima/stats`.
     - `TCP.ForwardMaxInFlightDrop` above 0 points to the in-flight cap.
     - `CurrentConnected` far above `CurrentEstablished` is a large TIME_WAIT set.
   - **Ideas, not made:**
     - a doctor row or supervisor log line over the same counters;
     - the guest's `ss -tn state syn-sent dst 192.168.5.2` during a stall;
     - #125's proxy-liveness log, set beside the incident window.

**Not worth it:**

| setting | why |
| --- | --- |
| `tcp_syn_linear_timeouts` | 68 s becomes 60 s |
| a wider `ip_local_port_range` | no measurable change |
| `tcp_tw_reuse` | it governs the guest's own TIME_WAIT, which isn't involved |
| an RST from the guest to clear the TIME_WAIT | gVisor ignores RSTs in TIME_WAIT (RFC 1337) |

## Not done

- vzNAT and `lima: user-v2`, measured: the loopback-only rule, and user-v2 is the same code.
- The real proxy (mitmdump), and the share of real box traffic that closes host-first.
- Lima master / v0.9.0 built from source.
- `tcp_syn_retries=2`.
- The stopped-listener test on vz (it ran on QEMU, through the same host kernel).
- DNS during the in-flight freeze.
- A Linux or WSL2 host (there Lima's qemu default is slirp; a `vz` guest exists only on macOS).
- x86_64 guests.
- QEMU's throughput, CPU and mount costs.
- Anything on the affected VM beyond reading its two hostagent logs.

## Re-running

The scripts are in [lima-freeze-kit/](./lima-freeze-kit/); paths below are relative to it. The
full pcaps stayed in the guest's `/tmp/fz`.

1. **Start the VM.** `limactl start fy-spike-fwd`, or create one with
   `limactl create --tty=false --name fy-x --cpus 4 --memory 4 template:podman`. Then
   `sudo dnf -y install tcpdump` in the guest, and `limactl copy {client,canary,flood,probe}.py fy-x:/tmp/fz/`.
2. **On your computer, check the ports are free and start the origin and the poller.**
   `lsof -nP -iTCP:47300-47399` should list nothing. Then:
   ```sh
   python3 origin.py origin.jsonl 47300 47301 …
   ./hostpoll.sh ~/.lima/fy-x poll.jsonl 0.5
   ```
   Kill both by their own PIDs afterwards.
3. **Reproduce:**
   ```sh
   limactl shell fy-x -- python3 /tmp/fz/client.py 192.168.5.2 47300 500 8 --out /tmp/fz/r.jsonl
   ```
   - Freezes under ~3 s after start, at 68 s.
   - In `poll.jsonl`, `ForwardMaxInFlightDrop` stays 0 and `CurrentConnected` holds for 60 s.
   - Add `--abort` for the mitigation, `--nodata` for guest-first closes, `--retry 2` for re-dial,
     and `--rate R` with `1` worker for the threshold table.
4. **The in-flight freeze:** run `python3 probe.py 110 192.168.5.2:<port> 1.1.1.1:443 &`, then
   `python3 flood.py 192.0.2.1 80 12 95`. This needs a VM without foldyard's wall.
5. **QEMU:** `limactl create --tty=false --name fy-freeze-qemu fy-freeze-qemu.yaml`, then
   step 3.

Stop the VMs afterwards. Never point any of this at a foldyard VM, which forbids root in the guest
from your computer.
