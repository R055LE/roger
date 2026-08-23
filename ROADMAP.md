# Roadmap

[`BACKLOG.md`](BACKLOG.md) is explicit that it's *"not a feature wishlist — it's the
production-hardening layer."* This file is the other half: new capability, not hardening — ideas
for Roger doing more without the owner personally driving every step, and for growing a small,
personal-interest Discord server that doesn't have a lot going on day to day.

Community/culture planning (what to post about, tone, events — not code) lives separately in
[`community-discord`](https://github.com/R055LE/community-discord), since Roger and the server it
runs in are different things. This file is the "how Roger could help" side.

Same effort key as BACKLOG: **S** ≈ an afternoon, **M** ≈ a day or two, **L** ≈ multi-day.

---

## 1. Personal digest — **S/M** — *shipped*

A feed roundup DM'd to the owner only, separate list and schedule from the public digest. Directly
answers "I'm out of the loop" — reuses Digest's existing RSS/feeds/curation plumbing rather than
building anything new, per [ADR-style discussion in the
spec](docs/superpowers/specs/2026-08-18-personal-digest-design.md).

*Why it's first:* smallest of the four, reuses infrastructure that already exists and is already
tested, and is the most direct fix for the stated pain.

## 2. Proactive public content ("Spark") — **L** — *shipped*

The flagship. A new scheduled capability that posts unprompted to a public channel to spark
discussion — reacting to real fetched items (AI/tech headlines, story recs, art highlights, open
questions), not free-generated commentary that could confidently state something wrong. Same risk
shape as Digest (§9 of `ARCHITECTURE.md`): scheduled, no Discord message input in the path, posts a
message, nothing destructive. Feed content is bounded as untrusted data, the model has no tools,
and public output is strictly parsed and sanitized. See
[ADR-0011](docs/decisions/0011-spark-grounded-discussion-prompts.md).

## 3. Model allocation pass — **S** — *shipped*

Spark and Giga Brain each have their own model chain, token cap, and optional dollar cap, distinct
from admin's tool-calling chain. Spark shipped the last missing allocation with item 2.

## 4. Parked — self-serve commands for other members

Not a stated pain point today (the server's small, and the ask was about the owner's own bottleneck,
not delegating to others). Noted so it doesn't get lost if the server grows enough that it becomes
one.

---

## Deliberately not doing (for now)

- **Scraping non-RSS sources** for items 1/2. Feedparser-only, matching Digest's existing
  constraint. A site with no feed is a future idea, not a blocker.
- **A distinct model/budget for the personal digest.** It shares Digest's `MODEL_DIGEST` chain and
  `DAILY_TOKENS_DIGEST`/`DAILY_USD_DIGEST` cap — a second job, not a second brain. Revisit if the
  editorial angle (item 2 territory) ever gets folded into it.
