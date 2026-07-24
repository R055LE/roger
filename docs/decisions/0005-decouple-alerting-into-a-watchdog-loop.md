# ADR-0005: Decouple ops alerting into a watchdog loop

- **Status:** Accepted
- **Date:** 2026-07-23

## Context

Roger needed budget-threshold, permission-loss, and digest-failure alerts to the ops channel. Budget
could be checked inline in the LLM layer at the spend site; but permission-loss has no natural inline
trigger, and the LLM layer is deliberately kept ignorant of Discord (no import of the bot/client).

## Decision

Run a periodic **watchdog** `tasks.loop` (every 10 min) that reads live state — token usage,
permissions, last digest result — and pushes deduped alerts, rather than instrumenting alert calls at
each spend/permission site. Dedupe via a per-key cooldown (`OpsNotifier`).

## Consequences

- Alerting stays decoupled from the LLM and tool layers — no Discord coupling leaks into them.
- Permission-loss gets a home it wouldn't have had inline.
- Alerts are up to ~10 min delayed; acceptable for these conditions.
- A flapping condition can't spam the channel (cooldown), at the cost of in-memory dedupe state that
  resets on restart — fine, since startup re-evaluates and the boot report re-announces health.
