# ADR-0020 — Consumption model post-extraction: PyPI-first frozen installs; editable for foldyard development; opt-in co-dev mount; no vendoring

- **Status:** Accepted (2026-07-01), **amended 2026-08-29** (spinout-plan D-A supersedes D1): the
  default consumer install flips from a git install to **PyPI**, and cheap releases become the
  mechanism that keeps it fresh. Everything else here stands as decided — the editable developer
  flow, the box-side staging chain, the co-dev toggle (unbuilt) and no vendoring. The
  git-installed-host staging gap is demoted from blocking to optional by the same amendment.
- **Sources:** spinout-plan D1, amended by D-A

## Context

After extraction (ADR-0013), Tangible flips from host to consumer #1, and every consumer needs an
answer to: how do you get foldyard onto the Mac, how does the box get a matching copy, and how do
you hack on foldyard itself against a real consumer? The answer is constrained by ADR-0012's
rule — the host credential daemons must never run source a working tree can mutate — and by the
churn-period reality that fixes need to reach consumers quickly.

The original decision read that second constraint as "avoid release ceremony", and chose a git
install. The amendment reads it as "make releases cheap enough not to be ceremony", and chooses
PyPI — because the box-side staging chain below only works for three of the four host install
kinds, and the git one is the odd one out.

## Decision

Two flows, plus a box-side invariant.

**Just using** (teammates, any consumer):

    uv tool install foldyard

The install is **frozen at install time**: the daemons run a released snapshot no working tree can
mutate, which closes the consumer half of ADR-0012's mutable-source hazard by construction. Picking
up a fix is `uv tool upgrade foldyard && fy host` (plus `fy box up` if the fix is box-side) — a
bounce you would need after a change under any model, and one the fingerprint check (ADR-0012)
makes loud if forgotten.

**Why PyPI and not the git install this ADR originally chose.** The deciding constraint is how
`fy box up` gets a *matching* foldyard into the box (`src/foldyard/box.py`
`_foldyard_install_subst` → `stage_foldyard_for_box`):

| host install | box gets foldyard by | works |
| --- | --- | --- |
| repo **vendors** `foldyard/` (the pre-extraction monorepo) | `uv tool install --editable {checkout}/foldyard` | ✅ |
| **editable** sibling clone | host `uv build --wheel` → staged in `.devbox-foldyard/` | ✅ |
| **published** (PyPI) | `uv tool install "foldyard=={host version}"` | ✅ |
| **git** (`uv tool install git+https://…`) | falls through to the *published* branch | ❌ |

The git branch fails because `proxy._foldyard_src()` walks `parents[3]` looking for a
`pyproject.toml`; a git install is built into the tool venv, so it returns `None` and the box tries
`uv tool install "foldyard==<host version>"` against PyPI — a version that need not exist there.
PyPI-first is the branch that already works end to end, box included, offline, with no new code.

What makes that affordable is **cheap releases**: tag → `uv build` → `uv publish` from CI, a
release per merge to `main` through the churn period, so "you need to pull" never has to mean a git
install. The consumer repo pins a **floor**, not a version — `[project].min_foldyard_version` in
`foldyard.toml`, surfaced as a doctor row plus a one-line warning on `fy up`.

**One sharp edge this ordering creates, worth stating because it is silent.** Claiming the PyPI
name with a *placeholder* published at foldyard's current version would make every git-installed
host quietly install **the placeholder** rather than failing. Publish a placeholder only as
`0.0.0`, or skip the placeholder and go straight to a real release.

**Actively developing foldyard**: clone as a sibling repo and
`uv tool install --force --editable <clone>`. The CLI side is then genuinely live — every `fy`
invocation imports fresh source, so iteration is instant. The long-lived daemons hold what they
imported at start; ADR-0012's fingerprint-stamped lock makes a forgotten `fy host` bounce loud
instead of silent, and the addon is already immune via snapshot-at-launch.

**Box side — the box always matches the host, offline.** `fy box up` stages foldyard into the
checkout by a fallback chain (`src/foldyard/box.py` `_foldyard_install_subst` /
`stage_foldyard_for_box`): a repo that *vendors* foldyard (`<checkout>/foldyard/pyproject.toml`
exists — the pre-extraction monorepo case) installs editable from the mount; an **editable** host
install builds a wheel from its source into a gitignored `.devbox-foldyard/` under the checkout
(the machine mounts only repo + worktrees, so the mount is the only way in) — WIP-as-of-bake,
re-bake to iterate; otherwise a **published** host install pins the box to `foldyard==<host
version>`. **Known gap, optional under the amendment:** the chain does not handle a
*git-installed* host — it falls through to the PyPI pin and assumes the host's version exists
there. Blocking under the original git-first decision; under PyPI-first it is a nicety for outside
contributors who want to track `main`, off the critical path. It stays cheap when wanted:
`uv-receipt.toml` under `~/.local/share/uv/tools/foldyard/` already records the resolved source, so
the work is parse the receipt → clone/cache the rev → hand it to the existing
`stage_foldyard_for_box()` unchanged, keeping the box install offline.

**Opt-in co-dev mount** for tight in-box loops (built at Phase 5): a *declared*
extra machine mount of the foldyard checkout plus `uv tool install --editable <mounted>` in the
box. It must stay **verify-aware**: `verify`'s VM-boundary check is a deny-list today, so the
work is a positive mount-set assertion — machine mounts == {repo, worktrees root, declared co-dev
mounts} via `machine.mounts()` — so the declared mount is blessed while a stray one still fails.
Machine mount sets are init-only (neither podman `--volume` nor Lima `mounts:` can be edited
live — see `machine.py` `recreate`), so enabling the toggle costs a `machine recreate`; that
friction is why it is opt-in rather than default.

**No vendoring.** Tangible does not keep a copy of foldyard's source in its tree after the flip.

## Consequences

- Consumers get released-version stability with pinned-install safety; the upgrade path is one
  command plus the bounce, and `fy host` / `fy up` detect a half-done upgrade (new code installed,
  old supervisor running) and bounce it (ADR-0012). Freshness is bought with release cadence rather
  than with a main-tracking install.
- **The packaging ceremony moves ahead of the monorepo's flip to consumer, not after it.** Under
  PyPI-first the name, the licence and the metadata all have to be right *before* anyone can
  consume it — that is the cost this amendment accepts, and it is why the licence/metadata/release
  work is a gate rather than a follow-up.
- The box never needs network access to install foldyard: every rung of the chain resolves to a
  mount path, a staged wheel, or a version the image can carry — the one exception being the
  git-installed host, which the amendment moves off the critical path rather than fixing.
- foldyard developers accept a two-speed world: live CLI, launch-pinned daemons, re-baked box.
  The co-dev mount removes the re-bake for in-box iteration at the cost of a machine recreate to
  enable.
- Releases become routine rather than a milestone. The inversion is deliberate: under D1 a
  release signalled stability, under D-A it is the ordinary delivery mechanism, and the interface's
  stability is signalled by the version number instead.

## Rejected alternatives

- **git-subtree vendoring in Tangible** — puts foldyard's source back inside the consumer's
  working tree, making that tree the live source for the host credential daemons again: every
  consumer branch switch is back in the blast radius ADR-0012 closed, and it leaves two histories
  to reconcile on every sync in both directions.
- **`uv tool install git+https://…` as the default consumer route** — this ADR's own original
  decision (D1), superseded by D-A. It is frozen-at-install and ceremony-free, which is why it was
  chosen; it is also the single rung of the box staging chain that doesn't work, so every consumer
  would have hit a box that either failed to install foldyard or installed the wrong version. The
  fix was real but unbuilt, and inverting the order made it optional instead of urgent.
- **Editable-clone as the default consumer install** — the same hot-reload failure class: a
  `git pull` mutates source under running daemons. Editable is the *developer* flow, chosen
  knowingly, backstopped by the fingerprint bounce — not a default to hand teammates.
- **Permanent source mount into the VM** ("inject the library") — the box already gets foldyard
  injected, as a staged-wheel *snapshot*, which is the safe form of this idea; the live-view
  variant exists as the co-dev mount, deliberately opt-in and verify-audited.
