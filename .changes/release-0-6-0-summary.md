---
section: Summary
bump: minor
---

A security release: upgrade now. Two flaws let a dev box influence what your computer archives from
its agent transcripts: a planted link could make it copy other files on your computer into the
archive, and a planted session could be resumed by an agent on your computer in another project.
Both are fixed, and box Codex sessions now archive into foldyard's own Codex home instead of
`~/.codex/sessions`. After upgrading, on the Lima backend run `fy machine stop && fy up` once: every
Lima VM's boot provisioning changed, walled or not, and a running one is refused until it restarts
(the `podman` backend needs no restart). Then `fy box down && fy box up`, so the box picks up this
release.
