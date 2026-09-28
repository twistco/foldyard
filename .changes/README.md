# Pending changelog entries

Every change a consumer would notice adds **one file here**, in the same PR. `just release`
folds them into [CHANGELOG.md](../CHANGELOG.md) under the new version and deletes them. A
shared `## Unreleased` section made every pair of open PRs conflict; separate files never do.

Name the file after the change (the branch name is fine), e.g. `vz-revive.md`:

```md
---
section: Fixed
bump: patch
---

- **On macOS, starting a Lima VM no longer treats the running VM as an orphan.** Before a start
  foldyard reaps a Lima hostagent that outlived its VM, …
```

- **`section`** is one of `Security`, `Removed`, `Deprecated`, `Changed`, `Added`, `Fixed`. The
  release lists them in that order, and each section's entries in the order they were merged.
  `Summary` is the odd one out: a paragraph of prose (not a bullet) that goes above every
  section under no heading — for the release as a whole, like 0.3.2's "A security release:
  upgrade now". Usually added just before the release, if at all.
- **`bump`** is the smallest release this change can go out in: `patch`, `minor` or `major`.
  Before 1.0, a change that breaks config or CLI shape is `minor`. `just release` warns when
  the version it is asked for is a smaller step than a fragment says.
- **The entry** is one or more `- ` bullets, written as it should read in the changelog. Lead
  with what changed for the reader, in bold, then **why**. The changelog is what a consumer
  reads to decide whether to upgrade, and what they lift into their own
  `[project.foldyard_version_reasons]`.

`just check` validates every file here (`tests/test_changes.py`), so a malformed one fails the
PR rather than the release. Releasing: [docs/releasing.md](../docs/releasing.md).
