---
section: Summary
bump: minor
---

A security release: upgrade now. A compromised box could get code run on your computer through
foldyard's own git in `fy worktree add` and `remove` — the repo's hooks, its filter drivers, or a
planted partial-clone fetch. foldyard's git on your computer now runs none of them: a new worktree
is checked out, and `remove` checks it for uncommitted work, in a container from your box image,
so `fy worktree add` needs `fy box build` first. After upgrading, recreate each box once
(`fy box down && fy box up`); on a VM with the guest monitor, or on a Linux or WSL2 host (9p),
also `fy machine stop && fy up`.
