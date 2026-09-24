# Testing modes, TTLs, and credential checks — zero secrets

The interesting behaviour of [modes](./modes.md) — TTL expiry, dependent switches lowered along
with it, token services starting and stopping as the mode changes, credential checks going
DEGRADED and recovering — normally needs real credentials and real waiting to see. This rig
removes both: a **fake credential plugin** and a **mode clock** you can fast-forward. Everything
below runs on any computer with `fy` installed, with no secrets anywhere.

(A **mode** is a set of switches, each at a level. Terms are in the [glossary](./glossary.md).)

New to modes? Start with the [example project's mode
walkthrough](https://github.com/twistco/foldyard/blob/main/example/README.md#mode-walkthrough--modes-overlays-requirements-zero-secrets):
the same rig driving a real stack (a refused combination → the one-command fix → an overlay → a
visible feature). This page goes a level deeper: TTLs, the expiry cascade, and credential checks
lapsing and recovering.

## The pieces

**`[plugins.fakecred]`** — declare it in `foldyard.toml`, or in your gitignored
`foldyard.local.toml` to keep it off teammates' dashboards. It adds:

- a `fakecred` switch (`off | on | user`):
  - `on` runs the **fake token service**, a small HTTP server handing out dummy tokens (set up
    per project like the real ones, so leftover-process cleanup works on it too);
  - `user` is an **emergency level**: it has a TTL and switches itself off.
- a `fakedep` switch (`off | on`): `on` *requires* `fakecred` to be `on` or `user`. The
  plugin declares this as data (`Switch.requires`), the same way a real plugin or your
  `[[require]]` table would. So when `fakecred` expires, `fakedep` has to come down with it —
  that's the expiry cascade.
- a **credential check** (every 2 s) reading `~/.foldyard/<project>/fakecred-capability`:
  missing or `ok` means working; anything else means lapsed, and the content is shown as the
  DEGRADED detail. While lapsed, the fake token service's `/token` returns 403 — the same shape
  as a real credential that has expired.

**`fy clock`** — shifts foldyard's clock for everything that reads it: TTL expiry, countdowns,
the supervisor's automatic switch-off.

- `fy clock ff 2h` — fast-forward;
- `fy clock reset` — back to real time;
- `fy clock` — show the current shift.

Only your computer can change it; the offset is stored outside the repo, so nothing in the box can
shift it. The credential checks' *intervals* stay on real time, so fast-forwarding expires TTLs
without firing every check at once.

## The walkthrough

```bash
fy mode fakecred=user fakedep=on ttl=1h    # emergency level + a dependent switch
fy host restart                             # (or fy up) — starts the fake token service
curl -s http://127.0.0.1:$(fy mode | grep -o ':[0-9]*' | head -1 | tr -d :)/token
                                            # a dummy token, while the credential "works"

echo lapsed > ~/.foldyard/<project>/fakecred-capability
sleep 4      # the check runs every ~2 s — give it a tick or two
fy mode      # → fakecred user … ⚠ DEGRADED — fake capability lapsed (…) + the fix
fy state     # → a ✗ capability row; the supervisor log has the ok → DEGRADED change,
             #   and /token now returns 403: a lapsed credential, made visible

echo ok > ~/.foldyard/<project>/fakecred-capability   # "run the fix"
sleep 4      # same interval on the way back
             # → "capability … recovered" in the log; fy mode drops the DEGRADED marker

fy clock ff 2h                              # the TTL has now expired
             # within a tick the supervisor logs:
             #   TTL expiry strands dependent switches — settling fakedep=off
             #   TTL expired → fakecred=off, fakedep=off
             #   stopped fake-minter
fy mode      # → everything off, and consistent (fakedep came down WITH the expiry)
fy clock reset
```

Follow the supervisor log with `fy host logs -f` while you go.

## What each step tests

| step | what it exercises |
|---|---|
| `fy mode fakecred=user … ttl` | emergency levels, TTL recording, refusing combinations that can't work (all switches set together) |
| `fy host restart` starts the service | the supervisor reacting to the mode, starting a per-project token service |
| lapse file → DEGRADED | credential checks: plugin → supervisor → `capabilities.json` → mode mirror → `fy mode` / `fy state` |
| `echo ok` → recovered | logging the change, the dashboard recovering |
| `fy clock ff 2h` | expiry, the supervisor's switch-off, **the expiry cascade** (`devmode.settle_incoherent`), stopping the service |
| `kill -9` the supervisor, restart | cleaning up the leftover fake token service (`_is_our_daemon` matches its per-project path) |

The same plugin backs the unit tests (`tests/test_fakecred.py`, plus the check, cascade and clock
tests in `test_devmode.py` / `test_supervisor.py`): it is the live twin of those tests, not a
separate mechanism.

## DEGRADED vs changing the mode — the design line

A lapsed credential does **not** change the mode. The mode is what you *asked for*; whether the
credential works is *observed*. Lowering the mode on a lapse would lose what you asked for (you'd
have to notice and set it again after fixing the credential), flip back and forth on brief
failures, and turn a read-only check into something that writes. So a lapse shows as
`⚠ DEGRADED` on the level you asked for, with the fix in the message, and recovers in place.

The only thing that *does* write the mode is TTL expiry of an emergency level — your own
credentials must never quietly stay on — and the cascade that rides the same write: a dependent
switch the expiry would leave in a combination that can't work is lowered to its default too.
So an expiry always ends in a mode that works offline.
