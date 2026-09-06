# Testing modes, TTLs, and capability handling — zero secrets

The mode substrate's interesting behaviour — TTL expiry + the settle cascade, daemon
start/stop on posture change, capability probes degrading and recovering — normally needs
real credentials and real waiting to observe. This rig removes both: a **fake credential
plugin** and a **posture clock** you can fast-forward. Everything below runs on any host
with `fy` installed (validated end-to-end in a credential-less cloud VM), no secrets
anywhere.

First contact with the mode system? Start with the [example consumer's posture
walkthrough](../example/README.md#posture-walkthrough--modes-overlays-requirements-zero-secrets)
— the same rig driving a real stack (refusal → atomic fix → overlay → observable feature);
this page is the level below it (TTLs, the settle cascade, capability lapse/heal).

## The pieces

- **`[plugins.fakecred]`** (declare it in `foldyard.toml`, or your gitignored
  `foldyard.local.toml` to keep it out of teammates' dashboards). Contributes:
  - `fakecred` axis (`off | on | user`) — `on` runs the **fake minter** (a stdlib HTTP
    daemon serving dummy tokens, staged per-project like the real minters so orphan
    reaping works on it); `user` is an EMERGENCY rung (TTL + auto-revert, like `gcp=user`).
  - `fakedep` axis (`off | on`) — `on` *requires* `fakecred≠off` (a `mode_issues` error,
    like `llm≠off` requires `gcp=sa`), so a fakecred lapse exercises the settle cascade.
  - a **capability probe** (2s interval) reading `~/.foldyard/<project>/fakecred-capability`:
    missing or `ok` = capable; any other content = lapsed, shown as the DEGRADED detail.
    The fake minter's `/token` 403s while lapsed — the full "expired PAM grant" shape.
- **`fy clock`** — skews `devmode.now()` for every reader (TTL expiry, countdowns, the
  supervisor's revert). `fy clock ff 2h` fast-forwards; `fy clock reset` returns to real
  time; bare `fy clock` shows the skew. Host-only writes; the offset lives in the host
  state dir so nothing in the box can skew it. (Probe *intervals* deliberately stay on
  real time — a fast-forward lapses TTLs without stampeding every probe.)

## The walkthrough

```bash
fy mode fakecred=user fakedep=on ttl=1h    # emergency rung + dependent axis
fy host                                     # (or fy up) — starts the fake minter
curl -s http://127.0.0.1:$(fy mode | grep -o ':[0-9]*' | head -1 | tr -d :)/token
                                            # dummy token while capable

echo lapsed > ~/.foldyard/<project>/fakecred-capability
sleep 4      # the probe runs on a ~2s interval — give it a tick or two to report
fy mode      # → fakecred user … ⚠ DEGRADED — fake capability lapsed (…) + the fix
fy state     # → ✗ capability row; the supervisor log has the ok→DEGRADED transition
             #   and /token now 403s — the "silent 401s" incident, made visible

echo ok > ~/.foldyard/<project>/fakecred-capability   # "just gcp-elevate"
sleep 4      # same probe cadence on the way back
             # → "capability … recovered" in the log; fy mode drops the DEGRADED marker

fy clock ff 2h                              # the TTL is now lapsed
             # within a tick the supervisor logs:
             #   TTL expiry strands dependent axes — settling fakedep=off
             #   TTL expired → fakecred=off, fakedep=off
             #   stopped fake-minter
fy mode      # → fully offline, coherent (fakedep cascaded down WITH the expiry)
fy clock reset
```

## What each step is actually testing

| step | machinery under test |
|---|---|
| `fy mode fakecred=user … ttl` | emergency rungs, TTL recording, `mode_issues` atomic-set gate |
| `fy host` starts the minter | supervisor reconcile, staged per-project daemon launch |
| lapse file → DEGRADED | capability probes (plugin hook → supervisor loop → `capabilities.json` → mirror → `fy mode`/`fy state`) |
| `echo ok` → recovered | probe transition logging, dashboard recovery |
| `fy clock ff 2h` | expiry-on-read, the supervisor's durable revert, **the settle cascade** (`devmode.settle_incoherent`), daemon stop on posture change |
| kill -9 the supervisor, restart | orphan reaping of the fake minter (`_is_our_daemon` matches its staged path) |

The same rig backs the unit suite (`tests/test_fakecred.py`, plus the probe/settle/clock
tests in `test_devmode.py` / `test_supervisor.py`) — the plugin is the live-host twin of
those tests, not a separate mechanism.

## Degraded vs. changing the mode — the design line

A capability lapse does NOT change the mode. The mode is the *desired* posture (what you
asked for); capability is *observed* state. Auto-downgrading the mode on a lapse would lose
your intent (you'd have to notice and re-raise it after re-elevating), flap on transient
failures, and turn an observation channel into a write path. So a lapse renders as
`⚠ DEGRADED` on the desired rung with the fix in the message, and heals in place.

The one thing that DOES write the mode is structural: TTL expiry of an emergency rung
(your own credentials must never quietly persist — unchanged), and now the settle cascade
that rides the same write (a dependent axis the expiry would strand on an error combination
lands on its default instead). Expiry always resolves to a posture that works offline.
