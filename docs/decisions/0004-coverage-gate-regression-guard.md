# ADR-0004: Coverage gate as a regression guard at 75%

- **Status:** Accepted
- **Date:** 2026-07-23

## Context

CI ran `pytest` with no coverage gate. Total coverage is ~78%: the testable core (guard, store, llm,
schemas, digest) sits at 90–100%, but `bot.py`'s Discord I/O glue is ~46% — genuine integration-test
territory (it needs a fake gateway/client, not unit tests) that drags the total down.

## Decision

Gate CI at **75%** — below the current total — and omit the `__main__` entrypoint. Treat it as a
**regression guard, not a purity bar**. Do not chase the 80% aspiration by writing brittle tests
around Discord I/O.

## Consequences

- A green check honestly means "coverage didn't regress," not "everything is covered."
- Won't nuisance-fail when adding necessary-but-hard-to-unit-test Discord handlers (which keep landing
  in `bot.py`).
- If coverage ever breaches 75%, that's a real signal to invest in integration tests, not to lower the
  bar. Chose honest-and-useful over aspirational-and-red.
