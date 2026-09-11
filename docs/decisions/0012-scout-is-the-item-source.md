# ADR-0012: Scout is the item source for the scheduled brains

- **Status:** Accepted
- **Date:** 2026-09-08

## Context

Digest, the personal digest, and Spark all fetched RSS/Atom feeds themselves and handed whatever
arrived to a model. The prompt asks the model to summarize, which is a compression task: it
faithfully shrinks press releases along with everything else, and every item comes out as an
equal-weight bullet.

The digest for 2026-09-08 is the evidence. Fifteen items, of which one was worth reading. A CNCF
silver-member announcement, a Karmada graduation and a China cloud-native momentum report were
formatted identically to "Liberate your OpenClaw", which was the only item touching work actually
in progress. Nothing in the output distinguished them, because nothing in the pipeline had ever
decided one mattered more.

There are two separate faults. The feed list contains sources that publish announcements rather
than engineering material. And there is no relevance step at all, so a bad feed and a good feed
contribute equally.

Scout (`R055LE/agent-platform`, `scripts/scout`) already solves the second. It walks a watchlist,
scores each item against explicit weighted topics, records which topic and term matched, and writes
the survivors plus a near-miss list. It holds no credential, runs as a fixed operation, and writes
only to its own state directory.

## Decision

The scheduled brains read Scout's `digests/<run_id>.json` instead of fetching feeds. Scout is a
hard dependency of Digest, the personal digest and Spark.

Scout stays a separate tool rather than becoming a module here. Folding a digest builder into a
Discord bot means any future consumer, a different front end or a different messaging surface, has
to go through Discord to reach the data. Roger is the first consumer, not the only conceivable one.
Scout's output contract is documented in `agent-platform/docs/scout.md`.

The reason an item matched is passed to the model. That is the substantive change: the model
receives a short explained shortlist and can weight an item that matched three topics over one that
scraped past on a keyword, instead of compressing an undifferentiated feed dump.

Seen-state stays in this store at item granularity. That preserves the ADR-0011 interplay where a
successful Spark post marks only its chosen item seen, leaving the passed-over candidates eligible
for the roundup. Consumption is not tracked per run, because that would lose the distinction.

Roger reads a rolling window of recent digests rather than only the newest file. Scout suppresses
an item once it has reported it, so anything from a run Roger missed, through a failed post, a
restart, or a brain that did not fire, would otherwise be lost permanently. Overlapping windows are
free because the `seen` table deduplicates.

Missing or stale Scout output returns a distinct job status rather than "no new items". A producer
that stopped running must not read as a quiet day; the existing ops alerting keys off job status.

The mount is read-only. Roger consumes digests and cannot influence what Scout collects, which
keeps the trust direction one-way and means the container cannot leave anything in host state.

Scout is co-located on the deploy host as its own Compose service (`R055LE/scout`), following the
same shape as this repo and Hearth: GHCR image, cosign verification, and a poll timer. It was
prototyped in `agent-platform`, which is development tooling on a different machine; a production
dependency of this bot cannot live there. Co-location is what makes a plain read-only bind mount
sufficient and avoids inventing a host-to-host sync.

## Consequences

Tuning moves from Discord to a pull request. The feed-management tools (`add_feed`, `list_feeds`,
`remove_feed` and their personal variants) manage a list nothing reads any more, and are removed in
a follow-up change along with `feed_fetch.py` and the store's feed methods. Keeping that deletion
separate keeps this change reviewable and revertible on its own.

That is a real ergonomics cost on the surface that needs the most tuning, accepted deliberately:
the filter policy becomes a reviewable version-controlled file rather than runtime state nobody can
audit after the fact.

Roger no longer performs outbound HTTP for feeds, which removes a network egress path from the bot
and moves it to a tool with no credential and a host allowlist.

If Scout stops running, all three scheduled brains stop posting and say why. That is the intended
failure mode, and the reason the staleness threshold is configurable.

## Related

- ADR-0011 for Spark's grounding and the shared `seen` table.
- ADR-0009 for hosted inference over local serving; unchanged by this.
- `agent-platform` issue #39 and `docs/scout.md` for Scout's contract and phase gates.
