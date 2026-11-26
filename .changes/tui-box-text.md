---
section: Security
bump: patch
---

- **`fy tui` shows what the box chose as text, never as markup or terminal control.** A directory
  the box makes under the worktrees root gets a card of its own, and a name like
  `[@click=app.screenshot(…)]` turned that card's title into a click that wrote a file on your
  computer, at a path the box picked; a name the TUI couldn't parse, like `x[/`, stopped it from
  starting at all. Worktree names, branches, paths and the output of the verbs the TUI runs are now
  shown as plain text wherever it shows them, and so are a `[proxy] recommend` reason and an
  `[[inject]]` or `[[secret]]` label from your `foldyard.toml`.
- **Control characters the box sends no longer reach your terminal through the TUI.** An escape
  sequence in a request path, a User-Agent or an error body in the Network Log, in the supervisor's
  log, or in the Doctor tab could write your clipboard (OSC 52) or redraw the screen while the TUI
  was open. They are now shown as escapes, like `\x1b`.
