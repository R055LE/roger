# ADR-0010: Layer the dollar budget gate on the token gate, don't flip to it

- **Status:** Accepted
- **Date:** 2026-08-18

## Context

ADR-0001 split dollar-cost visibility from enforcement and predicted the natural follow-up: "flip
the gate to dollars with tokens as fallback." That follow-up landed differently. `DAILY_USD_<BRAIN>`
is optional and, when set, enforces *alongside* the token cap rather than replacing it — the token
cap still runs unconditionally on every call.

The reason is `cost_today()`. OpenRouter always returns a real `usage.cost`, but the field is an
OpenRouter extension, not a `usage` guarantee. ADR-0009 documents `OPENROUTER_BASE_URL` staying
pointed-but-configurable at a non-OpenRouter host. If that ever happens, `cost_today()` for that
brain sits at `0.0` forever. A gate that used dollars as the *primary* enforcement (tokens only as
fallback, per ADR-0001's plan) would need to detect "is cost data actually flowing" before it could
fall back — one more thing to get right, and wrong in exactly the case (a broken or misconfigured
cost feed) where the gate matters most.

Layering sidesteps that detection problem. The token check is unconditional and always runs first;
the dollar check is a second, independent, optional trip wire that runs after it, only when
`DAILY_USD_<BRAIN> > 0`. If dollar data never arrives, the dollar check simply never fires — no false
permissiveness, no code path that has to notice.

## Decision

Both caps enforce when both are configured. Whichever trips first wins. The token cap is never
replaced, only ever added to.

## Consequences

- A live host with a working OpenRouter key gets governance in the currency that actually matters —
  real spend — without losing the safety net a broken cost feed would otherwise remove.
- Two numbers to reason about per brain instead of one, in `/status`, the ops alert, and
  `roger.env.example`. Both default off, so this stays opt-in, not a forced complication.
- Supersedes ADR-0001's Consequences section, which predicted "flip... with tokens as fallback" —
  that mechanism was designed but not built, once the non-reporting-provider case above ruled it out.
  ADR-0009's cross-reference to "the existing dollar-cost tracking and token gate (ADR-0001)" should
  now be read alongside this record.
