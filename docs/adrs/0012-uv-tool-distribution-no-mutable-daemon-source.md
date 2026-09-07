# ADR-0012 — Distribution via uv tool install; long-lived daemons must not run mutable working-tree source

- **Status:** Accepted (distribution call 2026-06; daemon-immunity mechanism finalised 2026-07-03)
- **Sources:** HANDOVER.md (decisions 1/6), docs/history/spinout-readiness-review.md (Blockers 1 & 4),
  spinout-plan D1/D7

## Context

foldyard is two very different runtime shapes in one package: a CLI invoked fresh on every `fy`
call, and **long-lived host-side credential daemons** — the supervisor, the token minters, and the
mitmproxy egress proxy — that keep running between invocations and hold the project's security
posture.

The distribution question was settled early (HANDOVER decision 1): `uv tool install --editable`,
not `uv run --project`. Three reasons:

- The recipe hot path runs under system `python3`, which can't import typer/textual — so the hot
  path is **stdlib-only**, and Textual's ~290 ms import must stay **lazy** (it is; the TUI imports
  it on demand).
- A `foldyard/.venv` on the shared repo mount would thrash between Mac-built and box-built
  platforms; a **per-machine uv tool venv** (under `~/.local/share/uv/tools`, off any shared
  mount) avoids that.
- `foldyard` becomes an on-PATH entry point the dev-VM recipes can hard-depend on.

But an editable install means the interpreter loads source **directly from a working tree** — and
a working tree mutates. Observed 2026-06-29 (readiness-review Blockers 1/4): the egress-proxy
addon then lived at a consumer-repo path, and `mitmdump -s` **watches its script file**. A branch
switch rewrote the addon in place under the running proxy; mitmproxy's hot-reload un-loaded it and
left the proxy running as a **silent passthrough** — no token injection, no logging. The box's
keyless requests (which carry no real token, by design) sailed through unrewritten and the
upstream returned **401s**, while the proxy process stayed alive and listening, so nothing looked
down. The supervisor's restart trigger was the daemon's cmd+env signature, which does not include
script *content* — so the in-place edit was handled by mitmproxy's fragile in-process reload
instead of a clean respawn. The same class of bug applies to the supervisor process itself: it
computes daemon specs and minter allowlists from the code loaded at *its* start, and a branch
switch leaves it silently running stale Python (this bit for real: a Mac supervisor kept serving a
pre-update minter allowlist through repeated `fy host` / `fy up` calls, which just deferred to it).

## Decision

Keep `uv tool install` as the distribution mechanism (stdlib-only hot path, lazy Textual,
per-machine venv), and make it a hard rule that **no long-lived daemon ever executes mutable
working-tree source**. Two mechanisms enforce it:

1. **Snapshot-at-launch for the addon.** The addon is packaged at
   `src/foldyard/assets/proxy/egress_proxy.py` and the supervisor stages it to a stable launch
   path under `state_dir()` (`~/.foldyard/<project>/egress_proxy.py`) before **every** daemon
   (re)launch — `src/foldyard/supervisor.py` `_stage`, driven by the proxy plugin's daemon
   `stage` pair (`src/foldyard/plugins/proxy.py` `_launch_addon_path()`). `mitmdump -s` points at
   the snapshot, so mitmproxy's file watcher never sees an in-place edit: a git checkout can
   rewrite the packaged source all it likes; the running proxy's watched file is untouched, and a
   respawn re-snapshots fresh.

2. **Fingerprint-stamped singleton lock, replace-on-launch (D7, sharpened 2026-07-03).** At
   startup the supervisor stamps `pid + code-fingerprint` into its singleton lock file — the
   fingerprint is a hash over the *contents* (keyed by relpath + size) of every file in the
   package plus the interpreter path (`supervisor.py` `_code_fingerprint`), so any code edit flips
   it even when mtime is unreliable. The two launch paths (`fy host`, and
   `fy up` via `ensure_background`) compare the stamped fingerprint against their own, plus a
   heartbeat-age check for wedged reconcile loops (`_holder_stale_reason`), and **bounce** a stale
   holder instead of deferring to it: SIGTERM (its shutdown stops every child daemon cleanly) →
   wait for the flock to release → SIGKILL fallback, orphans reaped by the successor
   (`_bounce_holder`). `fy host --restart` forces a bounce unconditionally.

Post-extraction, consumers install **frozen at install time** (`uv tool install foldyard` from
PyPI — see [ADR-0020](./0020-post-extraction-consumption-model.md), amended to PyPI-first), which
closes the consumer half of the hazard by construction; the mechanisms above exist because
foldyard's own developers still run editable installs.

## Consequences

- A branch switch or `git pull` under an editable install can no longer silently degrade a
  credential daemon: the addon is immune (snapshot), and the next launch-path invocation replaces
  a stale supervisor loudly instead of trusting "it's running".
- The CLI stays instantly iterable for foldyard development (editable ⇒ every `fy` invocation
  imports fresh source); only the daemons are pinned to launch-time code.
- Residue (D7): there is no *live* in-flight warning yet — a stale supervisor that isn't touched
  by a launch path keeps running untouched between launches. The planned warn surface is a
  per-tick self-check feeding a TUI mode-page banner + a doctor warning.
- The fingerprint is mtime-based, so a `touch` triggers a bounce; that is a cheap false positive,
  never a false negative.

## Rejected alternatives

- **`uv run --project`** — puts a venv on the shared mount (platform thrash) and drags typer into
  the hot path; rejected at the original distribution decision.
- **Mid-flight auto-restart of the supervisor** (self-re-exec when it detects its own source
  changed). Rejected in D7 and kept rejected after the replace-on-launch sharpening: a **running
  supervisor is never interrupted between launches**. It owns live daemon children — a
  mid-flight token mint, open proxied connections — and a clean stop-children → re-exec →
  respawn handoff is real machinery to build and test for what is a developer-only condition
  (frozen consumer installs can never hit it). An editable-install file save must not blip the
  proxy under a live agent session; staleness is resolved at the next explicit launch, where a
  bounce is expected.
