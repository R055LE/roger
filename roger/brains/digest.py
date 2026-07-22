"""Digest brain: a scheduled RSS/Atom summary posted to one channel (§9).

No user input anywhere in this path. Runs on a daily ``discord.ext.tasks`` loop and is also
triggerable via the ``run_digest`` tool. One dead feed never kills the run; entries are marked seen
only after a successful post, so a failed post retries the same items next time.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Any
from zoneinfo import ZoneInfo

import discord
import feedparser

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
            parsed = await asyncio.to_thread(feedparser.parse, url)
        except Exception:
            log.exception("feed fetch failed: %s", url)
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


async def run_digest_job(*, client: Any, settings: Any, llm: LLM, store: Store) -> dict[str, Any]:
    feeds = settings.feeds
    channel_id = settings.digest_channel_id
    if not feeds or channel_id is None:
        return {"status": "digest not configured (set DIGEST_FEEDS and DIGEST_CHANNEL_ID)"}

    entries = await _collect_new(feeds, store)
    if not entries:
        return {"status": "no new items"}

    try:
        summary = await _summarize(entries, llm)
    except BudgetExceeded:
        log.warning("digest skipped: daily token budget hit")
        return {"status": "budget exceeded; skipped"}
    except LLMConfigError as exc:
        return {"status": f"digest brain not configured ({exc})"}

    channel = client.get_channel(channel_id)
    if channel is None:
        return {"status": f"digest channel {channel_id} not found"}

    today = datetime.datetime.now(ZoneInfo(settings.tz)).strftime("%Y-%m-%d")
    embed = discord.Embed(title=f"Roger's digest — {today}", description=summary[:4096])
    await channel.send(embed=embed)

    # Mark seen only after a successful post, so a failed post retries the same items.
    await store.mark_seen([(entry["feed_url"], entry["id"]) for entry in entries])
    return {"status": "posted", "count": len(entries)}
