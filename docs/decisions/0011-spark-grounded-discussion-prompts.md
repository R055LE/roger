# ADR-0011: Bound Spark's public output and give it its own budget

- **Status:** Accepted
- **Date:** 2026-08-19

## Context

Spark posts an LLM-written discussion prompt to a public channel without human review. Its source
items come from owner-curated feeds, but the titles, summaries, and item links are external data.
Spark and Digest also share the same feed list and `seen` table.

Three choices matter: how far the model can range beyond a source item, how untrusted feed content
reaches a public Discord post, and whether a spotlighted item should appear again in the roundup.

## Decision

Spark stays grounded in fetched items. The model receives a bounded JSON array of item numbers,
titles, and summaries, with instructions to treat every field as untrusted data. It has no tools.
Its response must match one fixed `ITEM:` / `BLURB:` / `QUESTION:` structure. Missing, duplicate,
out-of-order, empty, or out-of-range fields cause a clean skip.

Before delivery, Roger bounds every embed field, escapes mention text, disables allowed mentions,
and accepts only HTTP(S) item links without embedded credentials. The configured channel is
resolved before feed collection or the paid model call. Failed generation or delivery leaves the
item unseen and eligible for retry.

A successful Spark post marks only its chosen item seen. Spark defaults to 07:00 and Digest to
08:00, so the roundup skips the spotlighted item while retaining every candidate Spark passed over.

Spark gets its own model chain and daily token/dollar caps. Selection and question-writing have a
different quality/cost profile from neutral digest summarization, and separate accounting keeps one
job from consuming the other's daily allowance.

## Consequences

- Prompt-injection risk is contained to short public prose. Spark has no authority or tool surface.
- The ordering guarantee depends on deployments keeping `SPARK_HOUR` before `DIGEST_HOUR`.
- The shared `seen` table needs no migration, but Spark becomes a second writer to it.
- A feed item with a non-HTTP(S) or credential-bearing link can still be discussed, without making
  its link clickable.
