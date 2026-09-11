---
id: 0
type: fence
title: Keep r/mcp last even after adding bounded RSS retries
severity: medium
confidence: 0.9
created: 2026-09-10
authors: ["codex"]
anchors:
  - path: src/scout/reddit.py
    pattern: "max_rate_limit_retries"
  - path: scout.toml
    pattern: "subreddits"
evidence:
  - note: r/mcp returned 429 immediately during the 2026-09-10 live scout run
status: candidate
---

Bounded retries make a throttled Reddit feed more resilient, but they do not
make r/mcp cheap: every retry consumes time before later subreddits are read.
Keep r/mcp last so its retry/backoff window cannot starve the other feeds.
Do not remove the bounded retry cap or turn this into an unbounded retry loop.
