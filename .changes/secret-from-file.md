---
section: Changed
bump: patch
---

- **The prompt for a GitHub App's private key takes the `.pem` file's path.** Type it, or drag the
  file onto the terminal (or into `fy tui`'s dialog), and foldyard reads and encodes the key on
  your computer. The old hint had you run `base64 < key.pem | tr -d '\n'` and paste the output,
  which left the key in your terminal's scrollback and your clipboard: exactly what the hidden
  prompt is there to avoid. Pasting base64 still works, and `host.env` holds the same line either
  way. This applies to any `base64` secret, including a `[[secret]]` row's. A relative path isn't
  read, since the prompt runs in the checkout the box can write.
