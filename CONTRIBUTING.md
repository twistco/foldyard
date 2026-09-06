# Contributing to foldyard

Thanks for looking. Two things to know before you start.

**Where development happens.** Until `0.1.0` this repository is a *snapshot*: foldyard is still
developed inside the monorepo it is being extracted from, and `main` here is re-created from it
(one squash commit, force-pushed) as that work lands. Issues are welcome and read; a pull
request will be ported by hand rather than merged, and its history will not survive. After
`0.1.0` this becomes the home and that caveat goes away — `CHANGELOG.md` says which side of the
line we are on.

**How to work on it.** [`DEVELOPMENT.md`](./DEVELOPMENT.md) is the guide: the module map, the
test tiers (what runs anywhere, what needs an engine, what only runs on a real host), the
conventions, and the don't-break list. The short version:

```bash
uv tool install --force --editable ".[host]"   # the `fy` on your PATH runs your checkout
just check                                     # ruff + pyright + ty + the suite — what CI gates on
just test -k <pattern>                         # one area
```

- Design decisions live in [`docs/adrs/`](./docs/adrs/). A change that reverses one needs a new
  ADR that supersedes it, not an edit to the old one.
- New property-based tests are validated **red first** — mutate the code, watch hypothesis find
  the counterexample, revert. Commit before you mutate.
- Write "host", not "Mac"; the host being macOS is a fact about today's users, not the design.
- Security-relevant findings go to the address in [`SECURITY.md`](./SECURITY.md), not an issue.

By contributing you agree that your contributions are licensed under the Apache License 2.0
that covers the project (`LICENSE`).
