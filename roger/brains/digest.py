"""Digest brain: a scheduled summary of Scout's picks, posted to one channel (§9).

No user input anywhere in this path. Runs on a daily ``discord.ext.tasks`` loop and is also
triggerable via the ``run_digest`` tool. Entries are marked seen only after a successful post, so a
failed post retries the same items next time.

Items come from Scout (``roger.scout_source``), not from feeds fetched here. Scout scores against an
explicit watchlist and says why each item matched, so the model summarizes a short explained
shortlist instead of compressing whatever a feed happened to publish. See ADR-0012.
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import Any
from zoneinfo import ZoneInfo

import discord

from roger.llm import LLM, BudgetExceeded, LLMConfigError
from roger.scout_source import collect_from_scout
from roger.store import Store

log = logging.getLogger("roger.digest")

MAX_ITEMS = 15
_SUMMARY_CAP = 500  # per-entry summary chars fed to the model
_EPOCH = time.gmtime(0)

DIGEST_SYSTEM = (
    "You are Roger. Summarize these RSS/Atom items into a few short, grouped sections with terse "
    "bullets. No preamble, no sign-off, no filler. Keep the whole thing under ~300 words."
)


def _render_entry(entry: dict[str, Any]) -> str:
    """One item, with the reason Scout surfaced it.

    The reason is given to the model on purpose: it is the difference between
    summarizing a feed and summarizing a selection, and it lets the model weight
    an item that matched three topics over one that scraped past on a keyword.
    """
    why = ", ".join(
        f"{m.get('topic')}:{m.get('term')}" for m in (entry.get("matched") or [])
    )
    head = f"- {entry['title']} ({entry['link']})"
    if why:
        head += f"\n  [relevance {entry.get('relevance', 0)} via {why}]"
    return f"{head}\n  {entry['summary']}"


async def _summarize(entries: list[dict[str, Any]], llm: LLM) -> str:
    body = "\n".join(_render_entry(e) for e in entries)
    messages = [
        {"role": "system", "content": DIGEST_SYSTEM},
        {"role": "user", "content": f"Summarize these feed items:\n\n{body}"},
    ]
    response = await llm.complete("digest", messages)
    return response.choices[0].message.content or "(no summary)"


async def run_digest_job(*, client: Any, settings: Any, llm: LLM, store: Store) -> dict[str, Any]:
    channel_id = settings.digest_channel_id
    if channel_id is None:
        return {"status": "digest destination unset"}

    batch = await collect_from_scout(
        settings.scout_digest_path,
        store,
        max_age_hours=settings.scout_max_age_hours,
        limit=MAX_ITEMS,
    )
    if batch.status:
        # Scout is a hard dependency. Surface a broken producer rather than
        # reporting "no new items", which reads as a quiet day.
        return {"status": batch.status}
    entries = batch.entries
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
    batch = await collect_from_scout(
        settings.scout_digest_path,
        store,
        max_age_hours=settings.scout_max_age_hours,
        limit=MAX_ITEMS,
    )
    if batch.status:
        return {"status": batch.status}
    entries = batch.entries
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
