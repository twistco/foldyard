---
section: Fixed
bump: patch
---

- **With the VM firewall on, stack containers now skip the proxy for the names in `[proxy]
  no_proxy`.** podman gives every container the VM's proxy settings, but their `NO_PROXY` held
  only loopback and the host gateway. A service calling a sibling by name through a client that
  honours the proxy variables (curl, Python's `requests`, Google's Node clients such as
  `@google-cloud/storage`) sent the call to the proxy on your computer, which can't resolve a
  stack name and refused it: `fake-gcs:8000` showed up in the Network Log as a blocked host on
  every retry. That `NO_PROXY` now also holds your `[proxy] no_proxy` and foldyard's own services
  (the gcp metadata emulator), the same names the box gets. It is one list for the whole VM, set
  when it boots, so `{project}` is the main checkout's: a worktree's stack is covered by bare
  service names. After upgrading, `fy up` asks you to restart a walled VM (`fy machine stop &&
  fy up`); then run `fy down && fy up` so the stack's containers are recreated with it. A later
  change to `[proxy] no_proxy` takes the same two steps. See `fy docs networking`, "Stack
  containers trust the proxy too".
