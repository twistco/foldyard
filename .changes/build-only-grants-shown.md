---
section: Fixed
bump: patch
---

- **A grant that covers image builds only now says so, wherever you or the box meet it.** A host
  allowed at the build gate (`fy box build`, the stack build) reaches builds alone, but the TUI's
  allowlist listed it like any other grant and the box's refusals of it said "not on the allowlist":
  a box downloading Playwright's browsers at runtime looked refused despite its grant. The TUI's
  allowlist marks it "for image builds only", the Network Log's refused rows say "granted for image
  builds only", the box's 403 names the grant that would let it through (a path first, then the
  whole host), and the TUI's level picker warns that allowing such a host widens it to the box. The
  wall is unchanged.
- **A `fy allow add` command the proxy suggests, or a build prints without a terminal, is
  shell-quoted, and offers a path grant only when one would let the request through.** The path in
  it is the box's, and a path grant may hold `$ ( ) ;`, so pasting `fy allow add host/$(…)/` from a
  refusal or a build's output — or a host-side agent running it — would have run that command on
  your computer. A path grant passes GET and HEAD alone, so a refused POST or an off-port request is
  now offered only the whole host.
