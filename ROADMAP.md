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

## 2. Proactive public content ("Spark") — **L** — *idea only*

The flagship. A new scheduled capability that posts unprompted to a public channel to spark
discussion — reacting to real fetched items (AI/tech headlines, story recs, art highlights, open
questions), not free-generated commentary that could confidently state something wrong. Same risk
shape as Digest (§9 of `ARCHITECTURE.md`): scheduled, no user in the path, posts a message, nothing
destructive.

Needs its own design pass — content grounding, cadence, tone, and probably its own ADR given it's a
new class of unprompted public output. Not spec'd yet.

*Why it's not first:* biggest unknown, and the personal digest's grounding-vs-hallucination question
(item 1) is a smaller version of the same problem worth solving first.

## 3. Model allocation pass — **S** — *idea only*

Give content-generation brains (Spark once it exists, Giga Brain now) their own tuned/creative model
chain distinct from admin's tool-calling chain. There's real headroom to do this — Giga Brain's spend
has been low. Mostly config + evaluation, not new code.

*Why it's not standalone:* rides along with items 1 and 2 rather than being picked up on its own —
worth doing once there's actual creative-generation output to tune against.

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
