---
type: fence
title: r/mcp RSS throttles anonymous clients on first contact; keep it last in the subreddit list
severity: medium
confidence: 0.9
authors: ["kibukx"]
anchors:
  - path: scout.toml
  - pattern: "subreddits"
evidence:
  - commit: 3a1b8e3
  - note: measured 2026-09-10, r/mcp returned 429 as the second request at a 2s interval, and again on first contact minutes later at a 5s interval, while r/AIMemory returned 200 throughout
status: candidate
---

r/mcp is a large subreddit and its `/new.rss` feed throttles anonymous
clients immediately; the interval does not matter (429 at 2s and at 5s).
Other subreddits in the default list (r/AIMemory measured) serve fine at the
same rate.

The reddit source is specified to back off on 429 and stop for the rest of
the run. A throttled subreddit placed early in `subreddits` therefore starves
every subreddit after it. Keep `mcp` last so a 429 there costs one dead
source instead of five.

Do not "fix" this by retrying in a loop. The backoff-on-429 behavior is the
politeness design, not a bug.
