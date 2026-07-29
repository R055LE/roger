# ADR-0006: The admin tool-call budget stays a blast-radius wall, not a spend control

- **Status:** Accepted
- **Date:** 2026-07-29

## Context

`MAX_TOOL_CALLS` / `MAX_TURNS` (§2.9) are grouped with the security invariants in §2 — a hard
per-request ceiling meant to bound blast radius, not cost (§11 owns cost). At the old defaults
(5 calls / 8 turns), a single legitimate request — "remediate permissions for each category,"
spanning six categories — hit the wall in seconds. Actual spend that day was $0.072 total, nowhere
near the daily token cap; the wall had nothing to do with money. The synthetic error fed back to the
model also caused it to tell the user to "try again after the budget resets," implying a timed
cooldown that doesn't exist — a fresh request gets a fresh allowance immediately.

## Decision

Raise the defaults (5→10 tool calls, 8→14 turns) and make both env-configurable
(`ADMIN_MAX_TOOL_CALLS` / `ADMIN_MAX_TURNS`, read from `Settings` like the token caps) rather than
removing the wall or folding it into a dollar-denominated gate (that gate is a separate, still-open
question — ADR-0001). Fix the injected tool-error text so the model explains the cap accurately
instead of guessing. Add a `log.warning` plus a one-shot ops-channel post when a request actually
hits the cap, so real-world frequency is visible instead of silent.

## Consequences

- Multi-step admin requests have more headroom before hitting the wall, at negligible added cost —
  real per-call spend is cents, not dollars.
- The wall stays a hard, non-negotiable per-request ceiling; it does not become elastic just because
  dollars are cheap. An explicit continue/override for genuinely large jobs is a deferred idea
  (BACKLOG 1.7), not built.
- Confirm-gating (§2.8) already requires owner approval on every mutating call regardless of this
  cap, so raising it doesn't meaningfully change blast radius for owner-directed work — the wall
  now mostly guards against a runaway or hallucinating loop, not legitimate use.
- Ops-channel visibility means a "this keeps tripping" pattern is now observable, which should
  inform whether to raise the cap further or build the 1.7 override, rather than guessing.
