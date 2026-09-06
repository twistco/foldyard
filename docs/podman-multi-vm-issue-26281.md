# podman machine: one VM at a time on macOS (upstream podman#26281)

Reference note for the constraint that motivates the [Lima backend](./lima-backend-scope.md).
Cited by `machine_backend.py`.

## The constraint

On macOS, `podman machine` with the **applehv** and **libkrun** providers allows only **one
machine to be active at a time**. Starting a second while one runs fails with podman's
`only one VM can be active at a time`-class error — the providers gate on an
`RequireExclusiveActive` property. (This is a macOS-provider limitation, not a podman-wide one;
the Linux/QEMU path differs.) Upstream tracking: **podman#26281**.

The practical consequence for foldyard: two projects can't both have a live machine, so projects
are **mutually exclusive** under the podman backend.

## How foldyard handles it

`machine._start()` is gated on `BACKEND.supports_concurrent()`:

- **podman** → `False`: before starting, foldyard checks for another running machine and, rather
  than surface podman's cryptic error, **prints guidance and fails** — stop the other project's
  machine yourself, or switch to the Lima backend (`[machine].backend = "lima"`).
- **lima** → `True`: the guard is skipped; Lima runs VMs concurrently, so per-project machines
  coexist.

So the one-VM-at-a-time policy lives entirely in `machine._start()`; the backends just declare
whether it applies. podman is the portable, zero-dependency floor; Lima is the concurrency win.

## If upstream lifts the limit

If a future podman/provider release allows concurrent macOS machines, `PodmanBackend.
supports_concurrent()` becomes the single switch to flip (and the guard in `machine._start()`
self-disables). Re-confirm against the upstream issue before changing it.
