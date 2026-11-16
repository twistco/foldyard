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
