---
section: Fixed
bump: patch
---

- **With the VM firewall on, image build steps trust the proxy's CA.** A `RUN` step got the CA
  files but not the variables that point clients at them, so a build started inside the box
  (`fy up` there) failed `apk`, `curl`, `pip`, `uv` or `npm` against any host outside
  `passthrough`. The VM's boot setup now adds those variables to a build step, as it already did
  for running containers: an `ENV` in your Dockerfile still wins, and nothing is written into the
  image. GnuTLS clients (Debian's `apt` over HTTPS, `wget`) and Java still need the host on
  `passthrough` or the CA in the image.
- **The CA bundle containers get includes the usual public roots again on Fedora 44 VMs.** It
  held only the proxy's CA, so a client reading `SSL_CERT_FILE` or `REQUESTS_CA_BUNDLE` (`uv`,
  `pip`, `requests`) failed every `passthrough` host. Both land in the same one-time
  `fy machine stop && fy up` as the other CA changes.
