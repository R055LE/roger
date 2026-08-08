# ADR-0009: Keep Roger on hosted inference, and keep the local hook unused

- **Status:** Accepted
- **Date:** 2026-08-08

## Context

`OPENROUTER_BASE_URL` has always been config rather than a constant, on the
stated grounds that pointing Roger at a local inference host was an option worth
keeping open. The README still says so.

That option got tested. A separate spike lives in `R055LE/homelab` at
`discord-bot/`: about 120 lines, `discord.py` plus an `AsyncOpenAI` client
against a local llama-server on Thaddeus (a GTX 1070, 8GB VRAM). It was
deliberately minimal, one small system prompt and no tools, because that was the
only shape that ran acceptably on that card. Its own README records the pivot
off Hermes for the chatbot use case for the same reason.

What the spike established:

- Roger is far too heavy for a GTX 1070. The parts that make Roger worth running
  (tool calling, model fallback chains, cost tracking, the admin surface) are
  exactly the parts that don't fit.
- Once you add more than one concurrent user, response times people will
  actually wait for, and enough context to hold a useful conversation, the VRAM
  needed stops being available on that hardware.
- At that point the local option is being compared against OpenRouter's API
  pricing, and it does not come out ahead.

The constraint is the hardware, not the idea. `MODEL_ADMIN` in particular
requires tool calling, which narrows the field of models that would serve well
locally even with more VRAM.

## Decision

Hosted inference stays the only supported configuration.

`OPENROUTER_BASE_URL` stays configurable. The hook costs nothing and the
economics are a function of current hardware and current API pricing, both of
which move. This ADR records why it is unused, so the next person to look at it
gets the finding instead of re-running the spike.

The spike retires. Its findings live here rather than in a deleted directory.

## Consequences

- Roger has a hard dependency on network access and an OpenRouter budget. The
  existing dollar-cost tracking and token gate (ADR-0001) and the admin budget
  wall (ADR-0006) are what make that acceptable.
- Local inference on Thaddeus is not dead as a topic, it just isn't Roger's
  problem. `homelab`'s `llm-serving` component keeps it.
- Revisiting means new hardware or a materially different price curve, not a
  code change. The code is already ready.
- Two Discord bots stop being an undocumented duplication and become one
  product plus one retired experiment with its results written down. Tracked in
  R055LE/homelab#2.
