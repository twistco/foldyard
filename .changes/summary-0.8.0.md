---
section: Summary
bump: minor
---

A security release: upgrade now. A box could read your registry logins during a build, reach
services on your computer's loopback or LAN through the egress proxy, and ask your computer for git
credentials through `fy code`'s editor bridges. All three are closed, and the proxy now refuses
git pushes from the box. The release also drops the docker engine (CI included: see Removed), and
adds `fy agent` and path grants. After upgrading, run `fy host restart`: until the proxy and the
supervisor match, even an emergency `gh-cli` push is refused. Then run `fy box down && fy box up` so the
box picks up the new image, which now includes tmux.
