---
section: Changed
bump: minor
---

- **A worktree is always in its main checkout's project, so it shares main's state, ports and VM.**
  Its project and machine names now come from main's adopted config, adopted or not: under a main
  with no `[project].name` a worktree was named after its own directory, and one whose branch
  declared another `[project].name` became a project of its own, with its own `~/.foldyard/` folder
  and VM. What a worktree's own `foldyard.toml` says for those keys isn't read, and the review at
  its adoption and `fy doctor`'s `adopted config` row say so; a branch that renames the project
  takes effect once main adopts the rename. A box is given the names when it's made, so it names its
  stacks as your computer does: `fy box up` asks once for `fy box recreate` on a box made before. A
  worktree that was a project of its own leaves that VM and folder behind: `fy ls` lists them as
  left over, for you to delete.
- **Worktrees made with plain `git worktree add` aren't fy worktrees, and nor are Claude Code's
  under `.claude/worktrees/`.** You can go on using one with git, but on your computer every `fy`
  project verb run in it now refuses and names your main checkout, and `fy worktree add` won't make
  a worktree on a branch one has checked out. `fy box exec` still runs from one inside your main
  checkout, in main's box, so a commit there keeps running the git hooks that check it in the box.
  Move one into the worktrees root (`git worktree move`) and `fy worktree add <name>` registers it.
  A checkout made from a bare repository (a `.bare/` directory and `git worktree add main`) is
  refused the same way, saying so: use a normal clone as your main checkout.
- **Your computer decides which project a directory belongs to from its own records, never from git
  or a `foldyard.toml` there**: the mains you adopted, their registered worktrees and their
  worktrees roots. The box can write `.git` and `foldyard.toml`, and with them it could steer which
  project a verb on your computer acted on, and where its state went. Now what it writes can only
  make `fy` refuse, saying why: a registered worktree whose `.git` was deleted or made a repository
  of its own (`git worktree repair` mends it), a repository or `foldyard.toml` inside a checkout,
  the worktrees root itself, a directory two projects' roots share, and a path reached through a
  link that leads somewhere else.
- **A project, or its worktrees root, can no longer lie inside another one's checkout or worktrees
  root.** The outer project's box could write the inner one's files, and the inner one's box what
  the outer one reads, so neither wall held. `fy init` and `fy config adopt` refuse there, and
  around an adopted project; one adopted inside another's checkout before is skipped, named by
  `fy doctor` and refused where it is. A worktrees root on or around another project's checkout or
  root claims nothing and isn't mounted: that project's stack and box verbs and
  `fy machine recreate` refuse until it's changed, and `fy doctor` says what to change (a VM made
  before keeps its mount until `fy machine recreate`; `fy machine stop` stops what it runs). Two
  projects can no longer share one worktrees root: give each its own (`~/worktrees/<project>` is
  fine). To keep a nested project, move it out of the other one's checkout. A project in a
  subdirectory of a repository that isn't a fy project (`mono/svc`) works as before.
