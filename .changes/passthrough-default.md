---
section: Changed
bump: minor
---

- **The proxy now decrypts the toolchain too: `[proxy] passthrough` defaults to
  `["@jvm", "@linux"]` instead of `["@all"]`.** With the VM firewall on, the box, your stack's
  containers, image build steps and the VM's image pulls all trust the proxy's CA, and decrypting
  a toolchain install measured within a few per cent of tunnelling it, so `npm`, `pip`/`uv`,
  `cargo`, `go`, `git`, image pulls, the Claude Code installer and gcloud are now logged request by
  request. Only Java's own trust store (`@jvm`) and `apt` over HTTPS (`@linux`) stay tunnelled. A
  project that sets `passthrough` itself is unaffected. **To get the old behaviour back**, set
  `passthrough = ["@all"]` (or add just the bundle a tool needs, e.g. `"@vcs"`), then adopt it at
  the next `fy up`. The sign that a tool can't work decrypted is a certificate error naming the
  proxy's CA (`mitmproxy`, e.g. Java's "PKIX path building failed" or Chromium's
  `ERR_CERT_AUTHORITY_INVALID`). The box also sets `CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE`, so
  `gsutil` and `bq` trust the proxy like `gcloud` does.
