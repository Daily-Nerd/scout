---
type: fence
title: Example scar — this is what recorded negative knowledge looks like
severity: low
confidence: 1.0
authors: ["scar-init"]
anchors:
  - path: .scars/candidates/example-first-scar.md
  - pattern: "scar-example-inert-anchor-never-matches"
evidence:
  - note: seeded by `scar init` as a worked example
status: candidate
---

A scar records something a past attempt PROVED: an approach that failed
(deadend), weirdness that is intentional (fence), or a change here that breaks
something there (landmine). Agents get matching scars injected before they
edit anchored code — pain recorded once, never repeated.

This file is a live example of the format. The `path:` anchor points at a file
(this one); the `pattern:` anchor would fire when matching code appears in any
edit (this one is inert on purpose). `status: candidate` means it does NOT
inject yet — a human reviews and runs `scar promote example-first-scar.md` to
activate it, which assigns an id and moves it to `.scars/`.

Delete this file whenever you like — it exists only to be read once.
