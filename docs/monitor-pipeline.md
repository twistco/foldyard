# The guest monitor's host side: storage, filtering, the analysis pipeline and hooks

Status: **design**, 2026-10-04. Builds on [ADR-0032](./adrs/0032-a-root-collector-in-the-guest.md)
(slices 1–2: Tetragon in the VM, the signed spool, the host pull and join). What is built today:
the hook mechanism and its file sink (§6). Everything else here is proposed, with the measurements
it rests on.

## 1. What the events look like

From the slice-2 live runs on a Lima `template:podman` guest (Fedora 44, Tetragon v1.7.1,
`fy-observe` policy, `enable-process-ns`), a VM with a box and a stack, under QEMU TCG:

- **~7 events/s at rest**, ~2.1 KB each as stored (the full Tetragon event: the process, its
  parent, eleven namespaces). About **53 MB/hour** raw at that rate (7 × 2.1 KB × 3,600 s —
  derived from the two measurements, not measured itself).
- **60% of events are process exits.** The single largest source is `pasta` exiting (14%); then
  shell plumbing — `readlink`/`basename`/`ln`/`rm`/`grep`/`grepconf.sh` — from login shells and
  Lima's own boot scripts.
- **The monitor watched itself three times** before it was fixed: the policy covered the spool
  (fixed: inbox only), every pull logged in afresh (fixed: one multiplexed ssh connection), and
  each pull still runs `bash`, `find`, `sort`, `stat`, `tail` in the VM — ~120 events a pull,
  still open (§4).

## 2. Storage: what was measured

The same real records (9,692 from a clean boot; 72,001 from all runs) stored six ways. Sizes are
on disk; "q" is a two-minute time-window count and a group-by on binary × kind:

| store | B/event (clean / all) | ingest | q window / group-by | memory to open | install |
| --- | --- | --- | --- | --- | --- |
| JSONL, as today | 2,108 / 2,116 | — | full scan | — | stdlib |
| JSONL + zstd-3 stream | 52 / 24 | fast | full scan (decompress) | — | `zstandard` |
| JSONL, **zlib per 1,000-row batch** | **69 / 39** | 180–240 MB/s | per batch | — | **stdlib** |
| JSONL, lzma-6 | 39 / 17 | 12–20 MB/s | full scan | — | stdlib |
| SQLite, raw JSON + columns | 3,170 / 3,944 | 0.5 / 5 s | 0.3 ms / 18–142 ms | 11 MB | stdlib |
| SQLite, zstd per row | 939 / 1,044 | 0.2 / 2.9 s | 0.2 ms / 6–55 ms | — | `zstandard` |
| **chDB** 4.4.0, MergeTree, ZSTD(3) columns | **119 / 89** | 2.1 / 3.9 s | 5 ms / 4–8 ms | **403 MB** | **534 MB** + pandas + pyarrow |
| DuckDB 1.5.6 | (not sized) | | | 56 MB | ~25 MB |

**chDB is a strong analytical engine and a poor fit as foldyard's store**, for three reasons that
the measurements and its own docs agree on:

1. **One process per data directory.** A second process opening a store a writer holds is
   refused (`Cannot lock file …/status. Another server instance in same directory is already
   running`) — tested, and documented upstream (one data path per process; a session-level
   lock). The supervisor writes continuously, so `fy monitor`, the TUI and any hook would all
   have to read *through* the supervisor over IPC. DuckDB behaves the same way (a read-only
   open is refused while a writer holds the file).
2. **Footprint.** 403 MB resident just to open a session — in a supervisor that today runs in
   tens of MB — and 534 MB on disk plus pandas and pyarrow, for every foldyard install.
3. **It doesn't compress better than the standard library here.** MergeTree with ZSTD columns
   stored 89–119 B/event; zlib over 1,000-row batches of the same JSON stored 39–69 B/event. The
   records are highly repetitive JSON, and batch compression captures that as well as columns
   do.

What chDB (or DuckDB) *is* good for: querying immutable compressed files without owning them.
Both read gzip'd JSONL directly (`file('…/*.jsonl.gz', JSONEachRow)`), with no lock — so they fit
as an **optional engine for the analysis stages** (§5), not as the store.

**Proposed store — stdlib only:**

- **Segments:** append-only `JSONL` files, one per hour per tier, each batch the pull stores
  written as one gzip *member* (a valid gzip stream grows member by member; `zcat`, Python's
  `gzip`, chDB and DuckDB all read it). Closed segments are immutable. ~40–70 B/event.
- **A SQLite catalogue** beside them (WAL mode: one writer, any number of readers, including
  while the writer writes): which segments exist and what they cover, the rollups (§3), the
  exemplars, and each hook's cursor. Small, indexed, and the thing `fy monitor`/the TUI query.
- The supervisor is the only writer; every reader opens files and the catalogue read-only.

## 3. Tiers and retention

Budget: **1 GB per project** (configurable), at ~40–70 B/event compressed ≈ 15–25 M events of
full-fidelity history if nothing were thinned. Tiers, each a set of segments:

| tier | keeps | how long |
| --- | --- | --- |
| **everything** | every stored record: events, snapshots, gaps, engine claims | 8 h of activity |
| **most** | everything except the classes in §3.1 | 24 h of activity |
| **aggregates** | rollups per hour × worktree × container name × kind × binary (× destination for connects, × path for files): count, first/last seen, distinct args | until the budget |
| **exemplars** | the raw record of every event the aggregation found off-distribution (§3.2) | forever (own small budget, oldest-first beyond it) |

**Activity, not wall-clock.** Retention counts *hours in which events arrived*, and pruning runs
only at ingest — never on a timer. A finished session followed by silence keeps full fidelity
however long the silence lasts; it ages only as new activity accumulates behind it. (Alternative
considered: wall-clock age, pruned only when new events arrive — simpler, but the first event
after a weekend would prune the whole previous session at once.)

**Budget pressure** prunes in order: `everything` beyond 8 h → `most` beyond 24 h → aggregates,
coarsened (hour → day) from the oldest → exemplars beyond their own cap. Never the current hour.

### 3.1 "Everything" vs "most" — proposal

`most` drops what is high-volume and nearly never the question, and keeps every event that names
a security-relevant action:

- **dropped:** exits with status 0 and no signal (60% of all events; the exec is kept, so "what
  ran" survives — only "it finished normally" is lost), and `vm`-namespace plumbing execs whose
  binary *and* parent are on a short, packaged list (shell init helpers under a login shell,
  podman/conmon/crun internals).
- **always kept:** every exec in a container or `unattributed`; every connect; every file event;
  non-zero exits and signals; snapshots, gaps, forgeries, engine claims.

Expected: roughly a third of the raw volume. To be checked against real sessions before it's
the default.

### 3.2 Off-distribution exemplars

Kept forever, from what the aggregation sees, per worktree (and, where it holds, per container
name — a claim, so never the only key):

- first time a binary runs; first destination (IP:port, later joined to the proxy's hostname);
  first touch of a watched path; first `unattributed` container;
- a count far above that key's rolling hourly baseline (e.g. > p99 of the last 7 active days);
- every refused file access, every gap, every forged line.

Each exemplar keeps a little context: the records ±N seconds around it from the same container,
so a later review sees what led up to it.

## 4. Filtering at source

Tetragon filters its export before anything is written (`export-denylist`: binary, parent and
ancestor binary, arguments, event type, CEL expressions) and can strip fields from what it keeps
(`field-filters`). Proposed defaults, packaged with the policy (so a change re-provisions the VM,
and an invalid filter stops Tetragon — validated in the host-tier e2e like the policy):

- **The monitor's own transport:** the pull runs one packaged reader
  (`/usr/local/libexec/fy-monitor-spool`, installed by the boot script) instead of a shell
  pipeline, and that reader and its `bash -c` wrapper are denied by argument match. Removes the
  last self-observation loop (~120 events per pull).
- **`pasta` exits** (14% of events; `pasta` is rootless networking plumbing — its *connects* are
  kept, they are the real egress).
- **Fields:** keep `process.ns.mnt` (attribution needs it) and drop the other ten namespaces;
  keep the parent's `exec_id` and `binary`, drop the rest of the parent object. Roughly halves an
  event before compression.

Not filtered at source: anything in a container. Source filters are lossy and can't be revisited;
thinning beyond plumbing belongs to the tiers, where `everything` still has it for 8 h.

## 5. The analysis pipeline

```mermaid
flowchart LR
    S["store\n(segments + catalogue)"] --> A["aggregate\nrollups + exemplars"]
    A --> SC["score\nrules + anomaly"]
    SC -->|"scored events"| D["decide\nJev: signal vs noise"]
    D -->|"highlights"| R["review\nLLM + context"]
    SC -. hook .-> H1["alert"]
    D -. hook .-> H2["triage"]
    R -. hook .-> H3["report"]
```

1. **Aggregate** (deterministic, in the supervisor): rollups and exemplars as in §3.
2. **Score** (deterministic first): rules — direct egress from a container (a proxy-bypass
   attempt; refused when the VM firewall is on), sensitive-file reads, refused accesses, forged
   lines, gaps — plus simple anomaly scores against each key's baseline. Output: the event, a
   score, and the reasons. Arithmetic and baselines stay in code (plan §8F).
3. **Decide** (a decision model, e.g. Jev's typed Choice/Score outputs): narrow questions over a
   compact bundle — the scored event, its exemplar context, the worktree's recent rollups, and
   trusted task context where it exists — with an explicit *insufficient evidence* answer.
4. **Review** (an LLM): the decision model's highlights with a little more context around each,
   written up for a person.

**Trust:** every string in an event — arguments, paths, binary names — is chosen by whatever ran
in the box. Stages 3 and 4 receive it as quoted data inside a fixed frame, never as instructions;
the evaluation (plan §8F) includes adversarial text in filenames and arguments. Names and
worktrees are claims (ADR-0032) and are labelled as such in every bundle.

None of stages 2–4 is built. Each is reachable today through a hook.

## 6. Hooks (built)

A hook is the operator's program or endpoint, called with batches of attributed events. It is
how a scoring script, a decision model or a notifier plugs in without being part of foldyard.

**Configuration is host-only**: `~/.foldyard/<project>/monitor-hooks.toml`, never
`foldyard.toml`. Repo config is writable by anything in the box: a `command` there would be host
code execution for the box (ADR-0023), and a `url` there would send every event wherever the
box chose. The file is read fresh each round; there is nothing to adopt.

```toml
[[hook]]
name = "score"                      # unique; keys the hook's cursor
command = ["/usr/local/bin/fy-score", "--json"]   # argv, absolute path, no shell
# or: url = "https://hooks.example.org/fy"      # POST, JSON body
# or: file = "/abs/path/events.jsonl"           # the built-in sink: one signed line per batch
# headers = { Authorization = "Bearer …" }      # url hooks only
stage = "events"                    # the only stage today; later: scored, decided, reviewed
kinds = ["exec", "connect", "file"] # optional filters; omitted = all
who = ["container", "unattributed"]
worktrees = ["main"]
timeout = 15                        # seconds
```

- **Payload** (stdin for a command, the body for a url): `{"schema": "fy.monitor.events/1",
  "project", "hook", "events": [...], "first": [boot, seq], "last": [boot, seq]}` — the same
  `Event` fields `fy monitor --json` prints. Besides the attribution (`who`, `container`, `name`,
  `worktree`), each event carries its ancestry: `ppid`, `parent`, `parent_args` (the one hop) and
  `exec_id`/`parent_exec_id` (the links to rebuild the whole tree from `exec` events) — how a
  scorer tells a container's own daemon from what a session ran.
- **Settled events only**: an event is delivered once a snapshot after it exists (or two minutes
  have passed), so its attribution won't change — and never ahead of an earlier event that hasn't
  settled (a VM restarted inside those two minutes would otherwise strand the old boot's tail).
- **At-least-once:** a hook's cursor advances only when a batch succeeded (exit 0, or HTTP 2xx).
  A failing batch is retried with backoff and, after five attempts, skipped and recorded. Each
  hook reads the store from its own position (the store file's inode and the offset where its
  last event ends), so a hook that fell behind catches up rather than skipping to the tail; only
  the store rotating that position out loses events, and the supervisor log says so. A NEW hook
  starts at the store's last 4 MiB (about the last 2,000 events), not its beginning: history
  stays in `fy monitor`, and a hook added to a full store isn't handed every past event at once.
- **Isolation:** commands run with a minimal environment (`PATH`, `HOME`, `LANG`, `FY_PROJECT`,
  `FY_HOOK`) — never the supervisor's, which holds every `host.env` secret — on their own thread,
  so a slow hook never delays the pull.
- **Managed from the CLI:** `fy monitor hook add NAME` — with `--file PATH`, `--url URL` or
  `-- /abs/program args…`, and optional `--who`, `--kinds` and `--worktree` filters — appends a
  `[[hook]]` block, after parsing the result the way the supervisor will (a hook it would skip is refused, and so is a filter value no event
  has); `fy monitor hook remove NAME` cuts one block out, and refuses — leaving the file alone —
  when the edit wouldn't parse to exactly the old document minus that hook (an inline array of
  hooks, say). Comments and hand edits survive both. Bare `fy monitor hook` lists each hook with
  its filters and delivery state; `fy doctor` has a row per hook (failing, or batches skipped →
  warn) and one saying when there are none. All of them run on the host only.

### 6.1 The file sink

The use that motivated it: an experiment in the box scores the monitor's events, so the box needs
them in a file it can read — a file in the checkout. The obvious hook, a command running
`sh -c 'cat >> "$1"'` on that path, has two flaws. A shell append follows symlinks, so the box
(which writes the checkout, and whose writes reach the host as the operator) plants `logs → ~` or
`logs/events.jsonl → ~/.zshrc` and aims host writes at any file the operator owns, with content
partly shaped by the arguments of what it ran. And the box can edit or cut the file it is scored
on. `file = "<absolute path>"` replaces that recipe:

- **No symlink is followed that the box could have made.** The sink walks the path from `/`, one
  `openat(…, O_DIRECTORY|O_NOFOLLOW)` per component relative to the one before, then opens the file
  `O_APPEND|O_CREAT|O_NOFOLLOW` and refuses anything but a regular file with one link (a FIFO is
  refused without blocking on it; a hard link would be another file's inode). Resolving the path
  once (`realpath`) and opening it later would be a race the box wins, and resolving through a link
  it planted before the hook existed would point the sink wherever it chose. A link on the way is
  followed only when **root owns it and the operator can't write the directory holding it**:
  nothing running as the operator could have placed or swapped it. That admits the system's own
  (`/tmp`, `/var` and `/etc` on macOS; `/var/run` on Linux) and nothing a box can plant through a
  mount. Any other link is refused with the resolved path to configure instead — the operator
  resolves it, once, knowingly.
- **Directories are never created.** A missing one is usually a mistyped path, and evidence
  written where nobody looks is worse than a refusal that the log, `fy monitor hook` and `fy
  doctor` show. The file itself is created (0644).
- **One write per line**, repeated only for a short write. A refused or failed write is a failed
  delivery: retried with backoff, skipped after five attempts, like any other hook.

**Each line is signed and chained.** The key is the sink's own (`<state dir>/monitor-sink.key`,
32 random bytes, 0600, made on first use) — not `monitor-relay.key`, which the guest relay holds.
A line is the batch body (`schema`, `project`, `hook`, `events`, `first`, `last`) plus

```json
{"sink": {"seq": 7, "prev": "<line 6's mac>", "mac": "<hex>"}, "schema": "fy.monitor.events/1", …}
```

where `seq` counts the hook's lines from 1 (kept in the hook's state, so it survives restarts),
`prev` is the previous line's mac, and `mac` is HMAC-SHA256 over the canonical JSON (sorted keys,
no spaces) of the line without `mac`. `sink` is written first, so even a torn line says which
line it was.

**`fy monitor check PATH [--hook NAME]`** verifies a file against the key and the hook's state:
every mac, the sequence from 1 with no gap, every `prev`, and that the file ends on the seq and
mac the supervisor recorded last — which catches lines cut from the end, the edit the chain alone
can't see. It prints the first problem with its line number and exits non-zero. Host only: the
box has no key, and that is the point. At-least-once delivery leaves two shapes that are not
tampering, and pass with a note:

- **a torn line** — a write that failed part-way. The sink ends a file that doesn't end in a
  newline before writing again, so the fragment is a line of its own; it must be the start of
  the line that was due (or of a re-send), and that line must follow it. A fragment at the very
  end is a delivery still being retried.
- **a re-sent seq** — written, but the supervisor stopped before recording it: the same seq again,
  chained from the same `prev` (it may carry more events). The next line chains from the re-sent
  one, and so does the recorded state.

The box can still read the file (the point), and it can still damage it — truncate it, delete it,
fill the disk. The check makes that visible; it doesn't prevent it. A read-only mount the box
gets the events through is the structural fix, scoped separately.

## 7. Open questions

1. **Retention clock:** active-hours (proposed) or wall-clock-pruned-on-ingest?
2. **"Most":** is dropping clean exits plus packaged plumbing the right default, or should `most`
   also drop `vm` execs generally?
3. **Exemplar thresholds:** the p99-of-7-active-days baseline is a placeholder; real sessions
   should set it.
4. **Where scoring runs:** in the supervisor (cheap, always on) or as a hook (replaceable)?
   Proposed: the deterministic rules in the supervisor, models behind hooks.
