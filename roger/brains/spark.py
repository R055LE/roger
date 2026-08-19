"""Spark brain: one grounded item + a discussion question, posted to a public channel (roadmap
item 2 -- see docs/superpowers/specs/2026-08-19-spark-design.md).

No user input anywhere in this path. Runs on a daily ``discord.ext.tasks`` loop and is also
triggerable via the ``run_spark`` tool. Reuses the same feed list and collection logic as the
digest; the only new step is asking the model to *choose* one item and write a question about it,
instead of summarizing everything.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any
from zoneinfo import ZoneInfo

import discord

from roger.brains.digest import _collect_new
from roger.llm import LLM, BudgetExceeded, LLMConfigError
from roger.store import Store

log = logging.getLogger("roger.spark")

SPARK_SYSTEM = (
    "You are Roger. Below is a numbered list of RSS/Atom items. Pick the ONE most interesting "
    "or discussable item -- favor items that raise a real question or invite an opinion over "
    "routine announcements. Respond in EXACTLY this format, nothing before or after it:\n\n"
    "ITEM: <number>\n"
    "BLURB: <2-3 short, deadpan sentences about the item, grounded only in its title and "
    "summary below -- never state anything about it that isn't in the text you were given>\n"
    "QUESTION: <one open-ended question inviting replies>"
)


class SparkParseError(ValueError):
    """The model's response didn't match the required ITEM/BLURB/QUESTION format."""


def _format_candidates(entries: list[dict[str, Any]]) -> str:
    lines = []
    for i, e in enumerate(entries, start=1):
        lines.append(f"{i}. {e['title']}\n   {e['summary']}")
    return "\n".join(lines)


def _parse_choice(text: str, n: int) -> tuple[int, str, str]:
    """Parse the model's ITEM/BLURB/QUESTION response. Raises SparkParseError on any mismatch."""
    fields: dict[str, str] = {}
    key: str | None = None
    for line in text.strip().splitlines():
        for prefix in ("ITEM:", "BLURB:", "QUESTION:"):
            if line.startswith(prefix):
                key = prefix[:-1]
                fields[key] = line[len(prefix) :].strip()
                break
        else:
            if key is not None:
                fields[key] += " " + line.strip()

    if not all(k in fields for k in ("ITEM", "BLURB", "QUESTION")):
        raise SparkParseError(f"missing field(s) in response: {text!r}")

    try:
        index = int(fields["ITEM"])
    except ValueError as exc:
        raise SparkParseError(f"ITEM was not an integer: {fields['ITEM']!r}") from exc
    if not (1 <= index <= n):
        raise SparkParseError(f"ITEM {index} out of range 1..{n}")

    blurb, question = fields["BLURB"].strip(), fields["QUESTION"].strip()
    if not blurb or not question:
        raise SparkParseError("BLURB or QUESTION was empty")

    return index - 1, blurb, question


async def _choose(entries: list[dict[str, Any]], llm: LLM) -> tuple[dict[str, Any], str, str]:
    """Ask the model to pick one entry and write a blurb+question. Raises SparkParseError."""
    body = _format_candidates(entries)
    messages = [
        {"role": "system", "content": SPARK_SYSTEM},
        {"role": "user", "content": body},
    ]
    response = await llm.complete("spark", messages)
    text = response.choices[0].message.content or ""
    index, blurb, question = _parse_choice(text, len(entries))
    return entries[index], blurb, question


async def run_spark_job(*, client: Any, settings: Any, llm: LLM, store: Store) -> dict[str, Any]:
    channel_id = settings.spark_channel_id
    if channel_id is None:
        return {"status": "spark not configured (SPARK_CHANNEL_ID unset)"}

    feeds = [row["url"] for row in await store.list_feeds()]
    entries = await _collect_new(feeds, store)
    if not entries:
        return {"status": "no new items"}

    try:
        chosen, blurb, question = await _choose(entries, llm)
    except BudgetExceeded:
        log.warning("spark skipped: daily token budget hit")
        return {"status": "budget exceeded; skipped"}
    except LLMConfigError as exc:
        return {"status": f"spark brain not configured ({exc})"}
    except SparkParseError as exc:
        log.warning("spark skipped: unparseable model response (%s)", exc)
        return {"status": "unparseable response; skipped"}

    channel = client.get_channel(channel_id)
    if channel is None:
        return {"status": f"spark channel {channel_id} not found"}

    today = datetime.datetime.now(ZoneInfo(settings.tz)).strftime("%Y-%m-%d")
    embed = discord.Embed(
        title=chosen["title"], url=chosen["link"] or None, description=blurb[:4096]
    )
    embed.add_field(name="Discuss", value=question[:1024], inline=False)
    embed.set_footer(text=f"Spark -- {today}")
    try:
        await channel.send(embed=embed)
    except discord.DiscordException:
        log.exception("failed to deliver the spark post")
        return {"status": "delivery failed; not posted"}

    # Mark only the chosen item seen -- passed-over items stay eligible for the digest roundup.
    await store.mark_seen([(chosen["feed_url"], chosen["id"])])
    return {"status": "posted", "title": chosen["title"]}
