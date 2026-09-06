# ADR-0022 — The host runs the `foldyard.toml` it ADOPTED, not the one in the checkout

- **Status:** Accepted (2026-08-25) — implemented (`src/foldyard/configpin.py`,
  `devmode.worktree_config`, `supervisor._report_config_drift` + the `ensure_background`/`main`
  gate, `fy config status|diff|adopt|revert`, `tests/test_config_pin.py`).
- **Sources:** the reported symptom ("a Claude changed `[proxy]` settings and the host supervisor
  picked them up immediately"); [0023](./0023-no-host-executed-code-from-the-repo-mount.md),
  whose per-field closures this generalises; [0006](./0006-host-side-enforcement-single-supervisor.md)
  (host-side enforcement), [0009](./0009-monitoring-cooperative-enforcement-locked.md) (what capture
  is worth), [0019](./0019-consumer-repo-plugins-trust-model.md) (consumer code the host runs).

## Context

`foldyard.toml` — and the gitignored `foldyard.local.toml` that deep-merges over it — lives
**inside the mount**. Writing it is within reach of everything the threat model names: an in-box
agent, a package postinstall, a branch checked out to review.

The supervisor rebuilt every worktree's config from that working tree on **every 2 s reconcile
tick** (`reconcile_once` → `devmode.worktree_config` → `config.resolve(repo=…)`, an uncached
`_read_toml`), rebuilt its daemon specs from the result, and restarted any daemon whose `cmd`/`env`
had changed. So a repo edit reached the Mac's credential daemons within one tick, unattended, with
no host-side act at all. Two fields made that material:

- **`[proxy] passthrough`** → `PASSTHROUGH_HOSTS`, the hosts the proxy does *not* MITM-decrypt or
  request-log. An in-box edit exempts a host of its choosing from capture — the monitoring half of
  ADR-0009, switched off from inside the thing being monitored.
- **`[[inject]] host`** (and `header`/`query_param`/`path_prefix`) → an **already-armed** axis's
  token delivered to a host the checkout picked. `token_env` had been derived from the axis
  precisely so a rule couldn't reach *another* mechanism's secret (audit §12, fourth round); the
  destination was never constrained.

The audit had closed this class field by field — `[proxy] allow` and `default_deny` moved to the
host-side store, `[[inject]] token_env` derived, `[engine].cli` validated, `[plugins.github]
permissions` capped. Each fix was right and none of them changed the **channel**: while the file
itself is live input to the host, every field added to it is untrusted-by-default and the next one
re-opens the hole. `[proxy] passthrough` and `inject.host` are simply the two nobody enumerated.

## Decision

**Repo config is an offer, not an instruction.** The host reads a snapshot it adopted, kept under
`~/.foldyard/adopted/<checkout>-<hash>/` — outside the mount, for the same reason the mode file
and the allow-store live there. Keyed by the checkout PATH, not by `config.project()`: that
resolves `[project].name` out of the working tree, so a renamed checkout would find no adoption
and fall back to the tree — the pin unlocked by editing the config it pins.

1. **`devmode.worktree_config()` returns the ADOPTED toml** (`configpin.effective`). That is the
   funnel every host-side reader goes through — the supervisor's tick, the TUI, `fy state` — so
   pinning it there covers the daemons, the injection rules, capture/passthrough and the posture
   surface at once, with no per-field enumeration and no way for a new field to opt out.
2. **Drift is reported, never applied.** Each tick compares tree against pin and, once per change,
   logs it and (on the clean→drifted edge) posts the macOS notification; `fy doctor` carries a warn
   row. Silence would be worse than the old behaviour — "my config change did nothing" must not be
   a mystery.
3. **Adoption is an explicit host act.** `fy up` / `fy box up` / `fy host` run the gate before
   starting anything (`supervisor.ensure_background`, `supervisor.main`) and offer **adopt** /
   **revert** / **ignore for now**; ignore keeps the adopted copy and asks again next time. Without
   a TTY (the detached launch, CI) nothing is adopted. `fy config status|diff|adopt|revert` are the
   same operations as verbs.
4. **First sight adopts once, on a terminal only** — loudly, and ACKNOWLEDGED (Enter is enough).
   The pin is host-side, so nothing in the yard can manufacture that state; but "nothing adopted"
   is also what a LOST adoption looks like, and adopting on sight turns that into "the host took
   whatever the tree said". That is not hypothetical: moving the store to the path-keyed layout
   orphaned an existing pin, and an operator who answered `ignore` at `fy up` had the declined
   config silently adopted by `fy box up` seconds later. Hence three halves, not two — a store move
   reads the previous location rather than treating the checkout as new; a first adoption can't
   pass unseen; and with **no TTY it does not happen at all**. That last case has nothing to fall
   back on (an empty pin means `effective()` reads the tree), so the launch gate REFUSES rather
   than warning: `fy config adopt` on a terminal is the one-command fix, and it is in the message.

   A first adoption is also **measured against main**. On its own axis there is nothing to diff, so
   the report was a digest and a line count — which is not a review, on the one adoption that most
   needs one. But a worktree is a branch off main, and main is normally adopted already, so the
   answerable question is what this branch would grant that main hasn't already been granted. That
   comparison is against **main's adopted snapshot, never main's working tree**: main's file is
   inside the mount like every other one, so a tree baseline would let unreviewed config launder
   itself through a worktree ("no change vs main", while main's tree was rewritten in the box an
   hour ago). Usually it reports that the branch grants nothing new, which is what makes the
   keypress cheap; when it doesn't, the added `[proxy]`/`[[inject]]` lines are on screen at the
   moment of the decision. All three surfaces show the same thing — the terminal gate,
   `fy config diff` (which no longer dead-ends on an unadopted checkout it was itself pointing at),
   and the TUI's adopt modal.

   Where there is no baseline at all — main's own first adoption, a brand-new consumer, or a
   worktree cut before main was ever adopted — the fallback is the config **itself**, run through
   the same blank/comment filter: every config-carrying line as an addition against the empty pin.
   The filter is what makes that readable at a prompt (a typical consumer config is ~40%
   config-carrying lines, so a 190-line file prints ~60), and it is the same view the drift diff
   uses, so there is one renderer and one set of rules. No truncation here either. What the
   declarations *mean* stays a pointer to `fy config widenings` — the adoption report says what is
   in the file, not that `@all` is ~200 un-decrypted hosts.
5. **Adoption swaps a whole GENERATION.** Each adopt writes a complete snapshot into a fresh
   `<pin>/generations/gen-*`, renames it into place, and only then points `adopted.json` at it;
   `inspect()` reads exclusively from the generation the marker names. In-place writes were atomic
   per FILE but not across them, so a supervisor tick landing mid-adopt could merge the new
   `foldyard.toml` with the old `foldyard.local.toml` — and an interrupted re-adoption left that
   mixture marked adopted for good (marker-last only fails safe on the very first adoption).
6. **Fail safe, not fail open**: an unparseable pin — or a marker naming a generation that isn't
   there — resolves to `{}` / "never adopted" (no `[proxy]` ⇒ no injectors), never to the tree,
   mirroring `allowlist.default_deny`'s refusal to fall back to repo config when its store is
   damaged.

Scope is deliberate. The pin governs **what the host does**. Stack and box wiring (compose files,
`[box]` tools and bootstrap, ports) still read the working tree, because their blast radius is the
VM the yard already owns — the audit's triage question is "does this need host privileges?", and
that half doesn't.

## Consequences

- A config change now takes one keypress at the next `fy up` — real friction, deliberately placed
  at the moment the operator is already looking at the terminal, and the same shape as the
  `[[secret]]` capture prompt.
- Adoption lands **within a tick** with no supervisor bounce: the running supervisor re-reads the
  pin every tick, so the pin is a gate, not a freeze. That's also why the gate runs *before*
  `ensure_background`'s "a current supervisor already holds the lock" short-circuit.
- **Two things this does not claim.** Adopting is a human reading a diff, so it is exactly as
  strong as that reading; and foldyard is installed `--editable` from the consumer checkout
  ([ADR-0013](./0013-in-repo-carve-out-until-extraction.md)), so until the spinout the package's
  own code is box-writable and a Mac-side launch adopts it through the code fingerprint
  (`supervisor._holder_stale_reason`). The pin closes the unattended *config* channel; it does not
  make a box-writable checkout unable to influence the host at all. `docs/security.md` states both.
- A worktree keeps its own pin (its `foldyard.toml` may legitimately differ per branch), so
  switching branches in a checkout is drift and gets asked about — which is the intended reading of
  "config travels with the branch, trust doesn't".
- Pinning makes config inert until adopted; it does not make it legible, and an adoption diff is a
  poor place to learn that `@all` means ~200 un-decrypted hosts. `fy config widenings`
  (`exposure.py`, audit §13.1) is the pull surface for that, plus the doctor row that warns when a
  config carries a key foldyard no longer honours.

## Rejected alternatives

- **Constrain the two fields instead** (bundle-only `passthrough`; pin `[[inject]].host` when the
  axis is armed). Cheaper and frictionless, and both remain good defence in depth — but it is the
  same per-field pattern that left these two open for a year, so it can't be the primary fix.
- **A hash-ack trust store that fails closed until acked** (the audit's §6 option A, scoped to
  config): strictly stronger, and it makes a fresh clone start with no injectors and everything
  decrypted until an operator acks. Rejected as the default for the friction; the machinery here is
  the same shape if that tier is ever wanted.
- **Adopt automatically at `fy up`, banner only.** `fy up` runs dozens of times a day, so a
  poisoned tree present at the wrong moment is adopted with nothing but a line in the scrollback —
  the difference between a gate and a log.
