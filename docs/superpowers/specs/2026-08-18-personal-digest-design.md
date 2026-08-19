# Personal digest — design

Tracks `ROADMAP.md` item 1. A feed roundup DM'd to the owner only, separate feed list and schedule
from the public digest (§9), so staying current doesn't depend on the owner going and looking for
news themselves.

## Goal

Reuse Digest's existing mechanism as a second job rather than building anything new: same
feedparser/dedup logic, same curation-tool shape, same delivery pattern Giga Brain already uses for
private, owner-only output. The only genuinely new things are a second feed list and a second
schedule.

## Storage

New `personal_feeds` table, identical shape to the existing `feeds` table:

```sql
CREATE TABLE IF NOT EXISTS personal_feeds (
    url      TEXT PRIMARY KEY,
    title    TEXT,
    added_ts REAL NOT NULL
);
```

Purely additive (`CREATE TABLE IF NOT EXISTS`) — no migration of existing tables, no risk to live
data. `seen` stays exactly as-is and is shared between both lists: dedup is keyed on
`(feed_url, entry_id)`, which is already globally unique regardless of which list curated the URL.

Five new `Store` methods, each a direct mirror of the existing feed method of the same shape:
`list_personal_feeds`, `add_personal_feed`, `remove_personal_feed`, `seed_personal_feeds`,
`count_personal_feeds`.

This mirrors the codebase's own precedent for "same concept, second brain" — `ambient_log` /
`admin_log` / `gigabrain_log` are three separate tables with three separate method pairs, not one
table with a `brain` column. A `scope` column on `feeds` would work too, but duplication is the
established pattern here, so this follows it.

## Config

Three new settings in `roger/config.py`, next to the existing `digest_*` block:

```
personal_digest_feeds: str = ""          # seed, same shape as digest_feeds
personal_digest_channel_id: int | None = None   # unset = DM the owner
personal_digest_hour: int = 7            # own schedule, independent of digest_hour
```

`personal_digest_channel_id` unset means DM — same fallback shape `gigabrain_channel_id` already
uses, not a new pattern. A `personal_feeds` derived property (mirrors the existing `feeds` property)
splits `personal_digest_feeds` on commas.

No new model or budget settings. The job shares the `digest` brain's model chain
(`MODEL_DIGEST`) and daily cap (`DAILY_TOKENS_DIGEST` / `DAILY_USD_DIGEST`) — it's a second job, not
a second brain, per `ROADMAP.md`'s "deliberately not doing" note. Revisit if the editorial angle
(`ROADMAP.md` item 2 territory) ever lands here.

## `roger/brains/digest.py`

Two new functions, each a close mirror of an existing one:

- **`seed_personal_feeds_if_empty(store, settings)`** — copy of `seed_feeds_if_empty`, seeding from
  `settings.personal_feeds` into `personal_feeds` instead of `feeds`.

- **`run_personal_digest_job(*, client, settings, llm, store)`** — copy of `run_digest_job`'s
  fetch/dedup/summarize path (`_collect_new`, `_summarize` are reused as-is — both already take a
  feed list and a store, neither is Digest-table-specific), but:
  - Sources `store.list_personal_feeds()` instead of `store.list_feeds()`.
  - Delivery copies `run_gigabrain_suggestion`'s DM-or-channel pattern exactly: if
    `personal_digest_channel_id` is set, post there; otherwise `client.fetch_user(settings.owner_id)`
    → `create_dm()`. This replaces `run_digest_job`'s channel-required early return — "not
    configured" now means "no feeds," not "no channel," since a DM destination is always
    reachable in principle.
  - Same `BudgetExceeded` / `LLMConfigError` handling as `run_digest_job`, same "mark seen only
    after a successful post" ordering.
  - Embed title `"Roger's personal digest — {date}"` in place of `"Roger's digest — {date}"`.
  - Still calls `llm.complete("digest", messages)` — same brain identity, so it shares the budget
    per the Config section above.

## Curation tools

Four new owner-only tools under `roger/tools/`, mirroring `list_feeds` / `suggest_feeds` /
`add_feed` / `remove_feed` exactly — same `ToolSpec` shape, schema in `schemas.py`, executor in
`executors.py` (the existing feed tools don't touch `guard.py`, so neither do these), same "no
confirm-gating" (the originals aren't gated; nil blast radius applies the same way to a second
list):

- `list_personal_feeds`
- `suggest_personal_feeds` (validates a candidate feed against the live web before proposing it —
  same as `suggest_feeds`)
- `add_personal_feed`
- `remove_personal_feed`

Registered the same way the existing four are — available to the admin brain, owner-gated by the
existing `user.id == OWNER_ID` check before any tool runs (§2.3), nothing new in the trust boundary.

## Scheduling

`bot.py` gains `_personal_digest_loop`, a `tasks.loop` matching the existing `_digest_loop` /
`_gigabrain_loop` shape: default `time=datetime.time(hour=7)`, `change_interval`'d to
`settings.personal_digest_hour` at startup (mirrors how `digest_hour` and `gigabrain_hour` are
wired), `before_loop` waits for the client to be ready. Calls `run_personal_digest_job` the same way
`_digest_loop` calls `run_digest_job`.

`seed_personal_feeds_if_empty` gets called at boot alongside the existing `seed_feeds_if_empty`
call.

## Docs

- `ARCHITECTURE.md` §9 gets a short addition noting the personal digest as a sibling scheduled job
  sharing Digest's mechanism and budget, plus a `personal_feeds` row in the §10 table.
- `ROADMAP.md` item 1 flips to *shipped* once this lands.
- `README.md`'s Status section: one line, matching how Digest is already described there.

## Testing

Extends `tests/test_digest.py` with personal-digest equivalents of the existing
`run_digest_job` / `seed_feeds_if_empty` cases (not-configured, posts-and-marks-seen,
budget-exceeded, dead-feed-doesn't-kill-the-run). New tool tests mirroring the existing feed tool
tests. `tests/test_config.py` covers the three new settings and the `personal_feeds` property.
`tests/test_compose.py`'s "every setting is forwarded" gate (the same one the dollar-budget-gate
work tripped) applies here too — the three new env vars need forwarding in `compose.yaml`.

## Out of scope

- Non-RSS sources (scraping). Feedparser-only, matching Digest's existing constraint.
- The "editorial" commentary angle — reacting/opining rather than summarizing. Noted for
  `ROADMAP.md` item 2, not this round.
- A distinct model or budget for this job (see Config).
- `/status` enrichment — the public digest doesn't surface feed count or last-run there today
  either, so this doesn't add scope beyond what already exists.
