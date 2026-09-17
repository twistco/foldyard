# Handover: the VM-backed host tier in CI — state after the 2026-09-17 follow-up session

Untracked (gitignored). Read `DEVELOPMENT.md` (CI section + tier 4), `docs/linux-support.md`
(the 2026-09-17 rows), `tests/e2e_host.py` first; then this.

## State (end of the second session, 2026-09-17)

- Branch **`ci/lima-host-tier`**, draft **PR #17**, main (with #16, the hermetic suite) merged
  in; `just check` green (1644 passed). Last opt-in run: **36/36 host-tier tests green, the test
  step 12 min, the job 15 min** — run 35244308703.
- Done this session: B (the home-path restart — finding CONFINED to a mount that IS the home),
  C (`test_reclaim_e2e.py`), D (`test_worktree_e2e.py`), E (`just census`, tests/tools/census.py),
  the `linux-support.md` rows for all of it. Verify now recreates the VM from its own copy (it
  audits the boundary it builds); the machine module's copy lives at `~/fy-e2e/machine/` and is
  left in place (the VM keeps mounting it).

## Open

### A. `native` — DECIDED and done (2026-09-17)

Dain chose "retire it, ADR first". ADR-0027 (`docs/adrs/0027-always-a-vm-native-backend-retired.md`
— 0026 was taken by main's vscode ADR, #18) + the docs sweep in one commit, the code/test removal
in the next (`NativeBackend` gone; `get_backend("native")` warns naming the ADR and returns podman;
the no-limactl message offers two VM ways out). PR #17 is READY for review with a refreshed body.
Main (#16, #18) merged in.

### F. Smaller follow-ups

- `linux-support.md` "Not yet validated": WSL2 (nothing measured), `fy tui`/`fy code`/`fy open`
  on Linux, the host wall's operator side (`sudo nft` sudoers recipe not written up).
- Squash the duplicate "test(wall): annotate…" commits at merge.
- A census of the host tier (`FOLDYARD_E2E=1 just census tests/test_*_e2e.py` on a Lima host,
  or add `--census` to the lima job and upload the JSONs as an artifact) — not run yet.

## How to run things

```bash
just check                                   # the unit/golden gate
just census tests/test_stack.py              # the subprocess report (paths, not a quoted -k)
FOLDYARD_E2E=1 uv run --group e2e pytest tests/test_e2e.py tests/test_*_e2e.py -v   # host tier, on a Lima host
gh workflow run foldyard-e2e.yml --ref ci/lima-host-tier                            # CI without a commit
```
