"""Digest brain: a scheduled RSS/Atom summary posted to one channel (§9).

No user input anywhere in this path. Runs on a daily ``discord.ext.tasks`` loop and is also
triggerable via the ``run_digest`` tool. One dead feed never kills the run; entries are marked seen
only after a successful post, so a failed post retries the same items next time.
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import Any
from zoneinfo import ZoneInfo

import discord

from roger.feed_fetch import FeedFetchError, fetch_feed
from roger.llm import LLM, BudgetExceeded, LLMConfigError
from roger.store import Store

log = logging.getLogger("roger.digest")

MAX_ITEMS = 15
_SUMMARY_CAP = 500  # per-entry summary chars fed to the model
_EPOCH = time.gmtime(0)

DIGEST_SYSTEM = (
    "You are Roger. Summarize these RSS/Atom items into a few short, grouped sections with terse "
    "bullets. No preamble, no sign-off, no filler. Keep the whole thing under ~300 words."
)


async def _collect_new(feeds: list[str], store: Store) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for url in feeds:
        try:
            parsed = await fetch_feed(url)
        except FeedFetchError as exc:
            log.warning("feed fetch failed: %s", exc)
            continue
        except Exception:
            log.exception("unexpected feed fetch failure")
            continue

        items: list[dict[str, Any]] = []
        for entry in parsed.entries:
            entry_id = getattr(entry, "id", None) or getattr(entry, "link", None)
            if not entry_id:
                continue
            items.append(
                {
                    "feed_url": url,
                    "id": entry_id,
                    "title": getattr(entry, "title", "(untitled)"),
                    "link": getattr(entry, "link", ""),
                    "summary": (getattr(entry, "summary", "") or "")[:_SUMMARY_CAP],
                    "published": getattr(entry, "published_parsed", None),
                }
            )

        unseen = await store.filter_unseen(url, [item["id"] for item in items])
        collected.extend(item for item in items if item["id"] in unseen)

    collected.sort(key=lambda item: item["published"] or _EPOCH, reverse=True)
    return collected[:MAX_ITEMS]


async def _summarize(entries: list[dict[str, Any]], llm: LLM) -> str:
    body = "\n".join(f"- {e['title']} ({e['link']})\n  {e['summary']}" for e in entries)
    messages = [
        {"role": "system", "content": DIGEST_SYSTEM},
        {"role": "user", "content": f"Summarize these feed items:\n\n{body}"},
    ]
    response = await llm.complete("digest", messages)
    return response.choices[0].message.content or "(no summary)"


async def seed_feeds_if_empty(store: Store, settings: Any) -> int:
    """One-time bootstrap: import DIGEST_FEEDS the first time the feed table is empty.

    After the initial seed the store is authoritative — Roger adds and removes feeds at runtime,
    and DIGEST_FEEDS acts only as the default set that returns if the list is ever fully cleared.
    """
    if await store.count_feeds() > 0:
        return 0
    return await store.seed_feeds(settings.feeds)


async def seed_personal_feeds_if_empty(store: Store, settings: Any) -> int:
    """One-time bootstrap: import PERSONAL_DIGEST_FEEDS the first time the table is empty.

    Same one-shot rule as ``seed_feeds_if_empty`` — after the initial seed the store is
    authoritative.
    """
    if await store.count_personal_feeds() > 0:
        return 0
    return await store.seed_personal_feeds(settings.personal_feeds)


async def run_digest_job(*, client: Any, settings: Any, llm: LLM, store: Store) -> dict[str, Any]:
    feeds = [row["url"] for row in await store.list_feeds()]
    channel_id = settings.digest_channel_id
    if channel_id is None:
        return {"status": "digest destination unset"}
    if not feeds:
        return {"status": "digest has no feeds"}

    entries = await _collect_new(feeds, store)
    if not entries:
        return {"status": "no new items"}

    try:
        summary = await _summarize(entries, llm)
    except BudgetExceeded as exc:
        log.warning("digest skipped: daily %s budget hit", "$" if exc.unit == "usd" else "token")
        return {"status": "budget exceeded; skipped"}
    except LLMConfigError as exc:
        return {"status": f"digest brain not configured ({exc})"}

    channel = client.get_channel(channel_id)
    if channel is None:
        return {"status": f"digest channel {channel_id} not found"}

    today = datetime.datetime.now(ZoneInfo(settings.tz)).strftime("%Y-%m-%d")
    embed = discord.Embed(title=f"Roger's digest — {today}", description=summary[:4096])
    try:
        await channel.send(embed=embed)
    except discord.DiscordException:
        log.exception("failed to deliver digest")
        return {"status": "delivery failed"}

    # Mark seen only after a successful post, so a failed post retries the same items.
    await store.mark_seen([(entry["feed_url"], entry["id"]) for entry in entries])
    return {"status": "posted", "count": len(entries)}


async def run_personal_digest_job(
    *, client: Any, settings: Any, llm: LLM, store: Store
) -> dict[str, Any]:
    """Like ``run_digest_job``, but sourced from the personal feed list and delivered privately.

    Delivery copies ``run_gigabrain_suggestion``'s DM-or-channel pattern: the configured channel
    if set, else a DM to the owner. Unlike the public digest, no channel is required to be
    "configured" — "not configured" here means "no feeds," since a DM destination is always
    reachable in principle.
    """
    feeds = [row["url"] for row in await store.list_personal_feeds()]
    if not feeds:
        return {"status": "personal digest not configured (no feeds)"}

    entries = await _collect_new(feeds, store)
    if not entries:
        return {"status": "no new items"}

    try:
        summary = await _summarize(entries, llm)
    except BudgetExceeded:
        log.warning("personal digest skipped: daily token budget hit")
        return {"status": "budget exceeded; skipped"}
    except LLMConfigError as exc:
        return {"status": f"digest brain not configured ({exc})"}

    channel_id = settings.personal_digest_channel_id
    if channel_id is not None:
        destination = client.get_channel(channel_id)
        if destination is None:
            return {"status": f"personal digest channel {channel_id} not found"}
    else:
        try:
            owner = await client.fetch_user(settings.owner_id)
            destination = await owner.create_dm()
        except discord.DiscordException:
            log.exception("failed to open a DM with the owner for the personal digest")
            return {"status": "DM failed; digest not delivered"}

    today = datetime.datetime.now(ZoneInfo(settings.tz)).strftime("%Y-%m-%d")
    embed = discord.Embed(title=f"Roger's personal digest — {today}", description=summary[:4096])
    try:
        await destination.send(embed=embed)
    except discord.DiscordException:
        log.exception("failed to deliver the personal digest")
        return {"status": "delivery failed; digest not sent"}

    # Mark seen only after a successful send, so a failed delivery retries the same items.
    await store.mark_seen([(entry["feed_url"], entry["id"]) for entry in entries])
    return {"status": "posted", "count": len(entries)}
