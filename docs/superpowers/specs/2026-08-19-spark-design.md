# Spark — Design Spec

Roadmap item 2 ([`ROADMAP.md`](../../../ROADMAP.md)): a new scheduled capability that posts
unprompted to a public channel to spark discussion, grounded in a real fetched item rather than
free-generated commentary. Same risk shape as Digest (§9 of `ARCHITECTURE.md`): scheduled, no user
in the path, posts a message, nothing destructive.

## Goal

Once a day, post one real feed item to a public channel with a short deadpan spotlight and an
open-ended discussion question — a different kind of post than the digest's everything-roundup,
aimed directly at "encourage more activity without me personally driving every post."

## Non-goals

- No new feed list or curation tools. Spark reads the same `feeds` table Digest already curates
  via `suggest_feeds`/`add_feed`/`remove_feed`/`list_feeds` — one list, two jobs reading it, same
  shape as personal digest reusing `_collect_new`.
- No editorial memory beyond dedup (no "avoid repeating this topic all month" logic).
- No reactions, threads, or follow-up handling — a single message post, nothing else.
- No DM fallback. A discussion prompt needs an audience; unlike the personal digest, an unset
  channel means the feature is simply not configured, not "fall back to DMing the owner."

## Architecture

New module `roger/brains/spark.py`, mirroring `roger/brains/digest.py`'s shape but with its own
brain identity (`"spark"`) and its own selection+prompt step in place of `_summarize`. It imports
`_collect_new` from `digest.py` rather than duplicating it — that function is already shared
between the public and personal digest jobs, and its dead-feed tolerance / `MAX_ITEMS` cap /
per-URL unseen filtering apply unchanged here.

```python
"""Spark brain: one grounded item + a discussion question, posted to a public channel.

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

_SUMMARY_CAP = 500  # matches digest.py's per-entry cap fed to the model

SPARK_SYSTEM = (
    "You are Roger. Below is a numbered list of RSS/Atom items. Pick the ONE most interesting or "
    "discussable item — favor items that raise a real question or invite an opinion over routine "
    "announcements. Respond in EXACTLY this format, nothing before or after it:\n\n"
    "ITEM: <number>\n"
    "BLURB: <2-3 short, deadpan sentences about the item, grounded only in its title and summary "
    "below — never state anything about it that isn't in the text you were given>\n"
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
    lines = text.strip().splitlines()
    fields: dict[str, str] = {}
    key = None
    for line in lines:
        for prefix in ("ITEM:", "BLURB:", "QUESTION:"):
            if line.startswith(prefix):
                key = prefix[:-1]
                fields[key] = line[len(prefix):].strip()
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

    blurb, question = fields["BLURB"], fields["QUESTION"]
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
    embed.set_footer(text=f"Spark — {today}")
    try:
        await channel.send(embed=embed)
    except discord.DiscordException:
        log.exception("failed to deliver the spark post")
        return {"status": "delivery failed; not posted"}

    # Mark only the chosen item seen — passed-over items stay eligible for the digest roundup.
    await store.mark_seen([(chosen["feed_url"], chosen["id"])])
    return {"status": "posted", "title": chosen["title"]}
```

Notes on the code above:

- `_format_candidates` numbers entries **1-based** in prompt order (`_collect_new`'s existing
  newest-first sort) rather than exposing the raw feed `entry_id`, which can be an arbitrary,
  sometimes-messy string unsuitable for the model to echo back reliably.
- `_parse_choice` is a small hand-rolled parser, not JSON mode — the OpenAI SDK wrapper (`LLM.complete`)
  has no `response_format` support today, and adding one is out of scope for a single call site. Any
  mismatch (missing field, out-of-range index, empty text) raises `SparkParseError`, which
  `run_spark_job` treats as a clean skip: nothing posted, nothing marked seen, retried next tick.
  Never guess which link a question is "about."
- Fetch-but-`feeds`-empty is folded into the same `"no new items"` status as "collected but nothing
  new" — both mean nothing to post, and neither is a misconfiguration once a channel is set.

## Config

New `Settings` fields (`roger/config.py`), grouped with the existing `--- digest ---` /
`--- personal digest ---` blocks:

```python
    # --- spark ---
    # No feed list of its own — reuses digest_feeds. Required channel, no DM fallback: a
    # discussion prompt needs an audience.
    spark_channel_id: int | None = None
    spark_hour: int = 7
```

`spark_channel_id` is added to the existing `_empty_to_none` validator tuple, same as
`personal_digest_channel_id`.

New brain identity `"spark"`, alongside `model_digest`/`daily_tokens_digest`/`daily_usd_digest`:

```python
    model_spark: str = ""
    daily_tokens_spark: int = 30_000
    daily_usd_spark: float = 0.0
```

```python
    @property
    def spark_models(self) -> list[str]:
        return _split_csv(self.model_spark)
```

## LLM layer (`roger/llm.py`)

`"spark"` joins the four existing brain keys in every per-brain dict:

- `_TEMPERATURE["spark"] = 0.6` — higher than digest's `0.3`. Digest summarizes neutrally; Spark
  writes an actual question, and a stiffer, more repetitive question every day defeats the point.
- `_MAX_TOKENS["spark"] = 400` — a blurb + one question is much shorter than digest's 1500-token
  roundup ceiling.
- `self._chains["spark"] = settings.spark_models`
- `self._caps["spark"] = settings.daily_tokens_spark`
- `self._usd_caps["spark"] = settings.daily_usd_spark`

No entry in `_reasoning_effort` — that passthrough is gigabrain-only today and Spark doesn't need
it.

## Scheduling (`roger/bot.py`)

Same `tasks.loop` shape as the other three jobs, conditionally started like the **public** digest
(not the personal digest's unconditional start) — `spark_channel_id` is boot-time deploy config,
never curated live the way feeds are, so gating the loop on it at boot carries none of the risk
that gating personal digest's loop on its feeds env var did.

```python
        if self.settings.spark_channel_id is not None:
            self._spark_loop.change_interval(
                time=datetime.time(hour=self.settings.spark_hour, tzinfo=ZoneInfo(self.settings.tz))
            )
            self._spark_loop.start()
            log.info("spark scheduled daily at %02d:00 %s", self.settings.spark_hour, self.settings.tz)
```

```python
    @tasks.loop(time=datetime.time(hour=7))
    async def _spark_loop(self) -> None:
        result = await run_spark_job(
            client=self, settings=self.settings, llm=self.llm, store=self.store
        )
        status = str(result.get("status", ""))
        log.info("scheduled spark: %s", status)
        problem = _spark_problem(status)
        if problem:
            await self._ops.alert(
                f"spark:{time.strftime('%Y-%m-%d')}",
                f"⚠️ **spark problem** — {problem}",
                cooldown_s=_DAY_S,
            )

    @_spark_loop.before_loop
    async def _before_spark(self) -> None:
        await self.wait_until_ready()
```

`_SPARK_OK_PREFIXES = ("posted", "no new items")` and `_spark_problem` mirror `_digest_problem`
exactly (not `_personal_digest_problem`'s list, which additionally OKs "not configured" because
that loop starts unconditionally — Spark's loop only starts once `spark_channel_id` is set, same
as the public digest, so its own "not configured" status is unreachable from the loop and doesn't
need an OK-prefix).

`_CONFIGURED_CHANNELS` gains `("spark_channel_id", "spark")` — the boot-time channel-reachability
check that the personal digest final review added retroactively for itself; Spark gets it from the
start.

**Default hour:** `spark_hour = 7`, same as `personal_digest_hour`, one hour before
`digest_hour = 8`. This ordering matters: Spark marking its chosen item seen must happen before the
digest roundup runs, or the roundup would (harmlessly, but redundantly) re-summarize an item Spark
already spotlighted. Two loops both firing at hour 7 is fine — they're independent tasks touching
the same table, and `aiosqlite`'s WAL mode serializes the writes; the ordering that matters is
Spark-before-Digest, not the two same-hour loops relative to each other.

## On-demand trigger

A `run_spark` admin tool, mirroring `run_digest` exactly — owner-only via the existing admin gate,
no `requires_confirm` (same risk class as `run_digest`: triggers a job that posts a message, no
destructive action).

`roger/tools/schemas.py`:

```python
class RunSparkArgs(ToolArgs):
    """No arguments — triggers the spark job immediately."""
```

```python
    "run_spark": ToolSpec(
        name="run_spark",
        description="Trigger the spark job (spotlight one feed item + a discussion question) immediately.",
        args_model=RunSparkArgs,
    ),
```

`roger/tools/executors.py`:

```python
async def run_spark(
    guild: discord.Guild, args: RunSparkArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    if ctx is None or ctx.settings is None:
        return {"status": "spark unavailable in this context"}
    from roger.brains.spark import run_spark_job

    return await run_spark_job(
        client=ctx.client, settings=ctx.settings, llm=ctx.llm, store=ctx.store
    )
```

Registered in `EXECUTORS["run_spark"] = run_spark`, next to `EXECUTORS["run_digest"]`.

## ADR

`docs/decisions/0011-spark-grounded-discussion-prompts.md`, covering:

- Why grounded-only (the model picks and reacts to a real fetched item; it never free-generates a
  discussion topic) — the same "the tool grounds it in reality" philosophy §9 already applies to
  feed curation, extended to Spark's selection step.
- Why Spark marks its chosen item seen and runs before the digest roundup, rather than leaving the
  digest to also cover it.
- Why Spark gets its own model/budget now (`MODEL_SPARK`, `DAILY_TOKENS_SPARK`) rather than sharing
  digest's, reversing `ROADMAP.md`'s earlier "not standalone" call on model allocation now that
  there's real editorial output — Spark's selection+question task benefits from a model tuned
  differently than digest's neutral summarization, and this is the "actual creative-generation
  output to tune against" that item 3 of the roadmap was waiting for.

## Docs

- `ARCHITECTURE.md` §9 (or a new §9a, whichever reads cleaner once drafted) gains a Spark bullet
  parallel to the existing personal-digest one; §10's table gains no new row (no new persistence);
  §7's tool registry table gains one row for `run_spark` (Mutates: no / posts a message; Confirm:
  no — same as `run_digest`'s row).
- `README.md`'s Status → Digest bullet gains a sentence, matching how the personal digest was
  folded in there.
- `roger.env.example` / `compose.yaml`: `SPARK_CHANNEL_ID`, `SPARK_HOUR`, `MODEL_SPARK`,
  `DAILY_TOKENS_SPARK`, `DAILY_USD_SPARK` forwarded (required by `tests/test_compose.py`'s
  bidirectional gate, same as every other brain/job setting).
- `ROADMAP.md` item 2 flips from *idea only* to *shipped* once this lands, same as item 1 did.

## Testing

Following `test_digest.py`'s style (fakes + a real `Store`, no mocks):

- `_parse_choice`: valid input at every candidate index; missing field; non-integer `ITEM`;
  out-of-range `ITEM` (too low, too high); empty `BLURB`/`QUESTION`; multi-line `BLURB` (the parser
  must accumulate continuation lines, not just the first line after the marker).
- `run_spark_job`: no channel configured → `"spark not configured"`, no LLM call made; no feeds →
  `"no new items"`; feeds present but nothing new → `"no new items"`; a normal run → posts an
  embed, marks **only** the chosen item seen (assert a passed-over item is still `filter_unseen`-
  eligible afterward); budget exceeded → `"budget exceeded"`, nothing posted, nothing marked seen;
  LLM config error → status reflects it; unparseable model response → `"unparseable response"`,
  nothing posted, nothing marked seen, item still collectible next run; channel not found → status
  reflects it; `channel.send` raising `discord.DiscordException` → `"delivery failed"`, nothing
  marked seen (mirrors the personal digest's `test_personal_send_failure_is_reported`).
- `run_spark` tool executor: no `ctx`/`ctx.settings` → `"unavailable in this context"`; delegates to
  `run_spark_job` otherwise (same shape as the existing `run_digest` executor test, if one exists —
  check `tests/test_executors.py` first).
- `_spark_problem`: OK-prefix statuses return `None`; everything else returns the status string
  (mirrors `test_ops.py`'s existing digest/personal-digest coverage).

## Out of scope (explicit)

- A `spark_feeds` table separate from `feeds` — decided against; see Non-goals.
- Multi-item posts, reactions, or thread creation.
- Tone options beyond deadpan (matches Ambient's established voice).
- Any change to Digest's or the personal digest's own behavior — Spark reads `feeds` and writes to
  `seen`, both already shared, and touches neither job's code path.
