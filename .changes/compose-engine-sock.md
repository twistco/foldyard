---
section: Added
bump: patch
---

- **On lima, a compose service can mount the VM's engine socket at `/run/foldyard/engine.sock`,
  the same path on every VM.** A service that talks to the engine, such as the gcp
  `metadata-emulator`, bind-mounts the VM's own socket, whose path holds the VM user's id
  (`/run/user/<uid>/podman/podman.sock`), so it differs from one developer to the next and
  compose files hard-coded one: `docs/gcp.md`'s example named `/run/user/1000/…`, wrong on
  macOS, and a project moved from podman machine kept `/run/docker.sock`, so every gcp mode switch
  failed on `statfs /run/docker.sock: no such file or directory` and the emulator never started.
  foldyard's boot script now links the fixed path to the right socket, root-owned so nothing in
  the VM can repoint it. Under `[machine] runtime = "gvisor"` it is the box's filtered socket, so
  a stack container holding it can't create a container outside gVisor. Write
  `/run/foldyard/engine.sock:/var/run/docker.sock` (docs/gcp.md shows it). The path is lima's
  only: on the deprecated podman machine backend keep `/run/docker.sock`, or under gVisor mount
  `/run/user/<uid>/podman/podman-runsc-filtered.sock`. A CI runner can link the path itself
  (docs/stack.md, In CI). **After upgrading, `fy up` asks you to restart each lima VM**
  (`fy machine stop && fy up`), since the boot script changed. Switching gVisor on or off now
  takes the same restart as well as the box recreate.
