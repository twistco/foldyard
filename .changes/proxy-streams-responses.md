---
section: Fixed
bump: patch
---

- **Streamed replies reach the box as they are sent, not all at once at the end.** The egress
  proxy held back every decrypted response of unknown length until 1 MiB had piled up or the
  upstream finished, so a Claude or Codex reply streamed over `text/event-stream` arrived in one
  piece when it was complete: no first token until then, and no keep-alive event either, so a long
  reply could trip the client's idle timeout. It now relays every successful response as it
  arrives, injected or not; on loopback the first of four events 0.8 s apart went from 2.4 s to
  0.01 s, the same as a direct connection. Error responses are still read whole, so a 401 is still
  re-issued with a fresh credential and the Network Log keeps its error snippet. `passthrough`
  hosts were never affected.
