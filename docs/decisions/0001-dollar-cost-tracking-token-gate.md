# ADR-0001: Track spend in dollars, keep enforcement on tokens

- **Status:** Accepted
- **Date:** 2026-07-23

## Context

The daily budget was a per-brain **token** cap. But each brain's model chain mixes models at very
different prices, so a token count is a weak proxy for the thing that actually costs money — dollars.
OpenRouter returns the real per-call cost on the response (`usage.cost`, always included now).

## Decision

Capture and surface real **USD** cost per brain (in `/status` and Prometheus). Keep the **enforcement**
gate on tokens for now; defer a dollar-denominated gate (`DAILY_USD_*`) to its own change.

## Consequences

- Cost visibility becomes honest immediately, at ~zero cost (the number is already on the response).
- Enforcement still runs on the proxy. A follow-up can flip the gate to dollars with tokens as fallback.
- Splitting visibility from enforcement kept the change reviewable and avoided altering *when the bot
  refuses to spend* inside a display-focused commit — that's a semantic change, not a display tweak.
