---
section: Fixed
bump: patch
---

- **In a walled VM, apt in a docker-compose build reaches the proxy.** Compose's classic builder
  (what it uses against podman, which serves no BuildKit) passes only the upper-case
  `HTTP_PROXY`/`HTTPS_PROXY` build args, and apt reads only `http_proxy`. So an apt `RUN` step
  in a compose build started from the box went direct, and the VM firewall refused it. The
  guest's build-step hook, which already adds foldyard's CA to every build step, now also adds
  each proxy variable in the spelling the step lacks. This changes the VM's boot provisioning:
  a running walled VM is refused as stale until `fy machine stop && fy up`.
