---
section: Security
bump: patch
---

- **A program in the box can no longer hide what it fetches behind the image-build marker.** With
  the VM firewall on, image builds reach the proxy as user `fy-build`, and the proxy tunnels them
  instead of decrypting them. Anyone could use that user, so the box could too: putting
  `fy-build:<anything>@` in its own proxy URL turned a logged request (path, tool) into a
  hostname-only row. The allowlist still applied. Now only a build carrying the secret foldyard
  creates for it on your computer is tunnelled. One consequence: a build started *inside* the box
  (`fy up` there) is decrypted like any box request, so a build step fetching an HTTPS host
  outside `passthrough` fails its certificate check. Build from your computer, or put the host on
  `passthrough`.
