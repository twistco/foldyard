---
section: Added
bump: minor
---

- **`cq = true` under `[claude]` and `[codex]` connects the agent to
  [cq](https://github.com/mozilla-ai/cq), Mozilla.ai's shared agent-knowledge commons, on the
  hosted cq.exchange.** The box gets a pinned, checksum-verified `cq` CLI, cq's MCP server and
  skill (for Claude also `/cq:reflect` and `/cq:status`), and session hooks. At session start the
  agent is told to query cq before a task and to propose what it learns. On Claude, a failed tool
  call also prompts a cq query before the retry. When you leave `fy claude` or `fy codex`, it asks
  whether to reflect on the session you just left. Your cq.exchange key never enters the box: the
  new `cq` mode switch has the proxy add it to requests to `api.cq.exchange`. With `cq=off`, cq
  reads the public commons anonymously and queues proposals locally until the switch is on. Nothing
  is written into your `~/.claude` or `~/.codex`. A running box needs `fy box down && fy box up`
  to pick it up. See `fy docs configuration`.
- **`fy cq backfill` proposes what past sessions learned.** Run in the box, it reads the Claude
  and Codex sessions already there, has a model of your choice look for what cq's reflect would
  propose, and proposes it to cq.exchange, where it waits for review. The model is set in
  `[cq] reflect_model` (default `haiku`), by `FOLDYARD_CQ_REFLECT_MODEL`, or for one run with
  `--model`. See `fy docs configuration`.
