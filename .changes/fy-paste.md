---
section: Added
bump: minor
---

- **`fy paste` sends an image from your computer into the dev box**, because an image pasted into an
  agent there never arrives: the box has no clipboard of yours. Run it on your computer with an
  image on the clipboard, or name a file (`fy paste shot.png`). The image lands in the box's
  `/tmp/fy-paste/` and its path is copied to your clipboard, for you to paste into the agent. With
  `--session <id>` it is typed into that `fy agent` session's prompt instead, and Claude attaches it
  as an image. Only a PNG, JPEG, GIF or WebP is sent, 20 MB at most, and only to this checkout's
  box. Reading the clipboard takes `osascript` (macOS), `wl-paste` (Wayland) or `xclip` (X11).
