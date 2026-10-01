"""Read Scout's digest output as the item source for curated news (§9).

Scout (``R055LE/agent-platform``, ``scripts/scout``) walks a watchlist of public
feeds, scores each item against explicit topics, and writes the survivors to
``digests/<run_id>.json`` with the reason each one matched. Roger consumes that
instead of fetching feeds itself.

Why this direction. The brains previously fetched feeds and asked a model to
summarize whatever arrived, which is a compression task: it faithfully shrinks
press releases along with everything else. Scout does the selecting and says
why, so the model gets a short explained shortlist rather than a firehose. The
digest for 2026-09-08 carried fifteen items of which one was worth reading, and
that is the failure this addresses.

Why Scout stays a separate tool rather than a module here. Folding a digest
builder into a Discord bot means any other consumer has to go through Discord to
reach the data. Roger is the first consumer, not the only conceivable one.

Trust. Everything in a digest originates from an external feed and is untrusted
quoted data, exactly as it was when Roger fetched feeds directly. The mount is
read-only; Roger cannot influence what Scout collects.

Availability. Scout is a hard dependency of curated news. When its
output is missing or stale the job reports that rather than silently posting
nothing, so the existing ops alerting sees a broken producer.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import logging
import pathlib
import time
from typing import Any

from roger.store import Store

log = logging.getLogger("roger.scout")

# Read a window of recent digests rather than only the newest. Scout suppresses
# an item once it has reported it, so an unread run's items never reappear; if
# Roger only looked at the latest file, anything from a run it missed (a failed
# post or a restart) would be lost for good. The store's
# seen table does the deduplication, so overlapping windows are free.
WINDOW_HOURS = 72
# Covers 72 hours at a 15-minute producer interval (288 runs), with room for
# restarts/manual runs. The cap still bounds a directory full of damaged files.
MAX_FILES = 512
_SUMMARY_CAP = 500  # bounds untrusted feed text before model input


@dataclasses.dataclass(frozen=True)
class ScoutBatch:
    entries: list[dict[str, Any]]
    newest_run_id: str | None
    age_hours: float | None
    status: str  # "" when usable, else a reason suitable for a job status


def _to_struct_time(value: object) -> time.struct_time | None:
    """Scout emits ISO-8601; collection sorts on ``time.struct_time`` like feedparser."""
    try:
        parsed = datetime.datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(datetime.UTC).timetuple()
    except (TypeError, ValueError):
        return None


def _aware_datetime(value: object) -> datetime.datetime | None:
    try:
        parsed = datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(datetime.UTC)


def _entry_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """Map one Scout item onto the dict shape the curated brain consumes.

    ``feed_url`` and ``id`` keep their names because they are the seen-table key
    and every call site already speaks that shape. ``feed_url`` holds Scout's
    feed id rather than a URL, which is a small lie in the name and a large
    saving in blast radius.
    """
    native_id = str(item.get("native_id") or "").strip()
    link = str(item.get("url") or "").strip()
    if not native_id and not link:
        return None
    feed = str((item.get("extra") or {}).get("feed") or item.get("source") or "scout")
    return {
        "feed_url": f"scout:{feed}",
        "id": native_id or link,
        "title": str(item.get("title") or "(untitled)"),
        "link": link,
        "summary": str(item.get("summary") or "")[:_SUMMARY_CAP],
        "published": _to_struct_time(item.get("published")),
        "published_at": (
            published.isoformat() if (published := _aware_datetime(item.get("published"))) else None
        ),
        # Carried through so the model is told why an item surfaced. This is the
        # whole point of consuming a scored source instead of a raw feed.
        "relevance": int(item.get("relevance") or 0),
        "matched": item.get("matched") or [],
        "article": item.get("article") if isinstance(item.get("article"), dict) else {},
        "observed_at": item.get("_scout_observed_at"),
    }


def _read_digests(
    digest_dir: pathlib.Path, now: datetime.datetime
) -> tuple[list[dict], str | None, float | None]:
    """Return items from recent digest files, newest run id, and its age in hours."""
    try:
        paths = sorted(digest_dir.glob("*.json"))
    except OSError as exc:
        log.warning("scout digest directory unreadable: %s", exc)
        return [], None, None
    if not paths:
        return [], None, None

    paths = paths[-MAX_FILES:]
    newest_run_id = paths[-1].stem
    cutoff = now - datetime.timedelta(hours=WINDOW_HOURS)
    items: list[dict[str, Any]] = []
    newest_started: datetime.datetime | None = None

    for path in reversed(paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Scout writes atomically, so this means a genuinely damaged file.
            # One bad digest must not cost us the readable ones.
            log.warning("skipping unreadable scout digest %s: %s", path.name, exc)
            continue
        started = _aware_datetime((payload.get("run") or {}).get("started_at"))
        if started is not None:
            if newest_started is None:
                newest_started = started
            if started < cutoff:
                break
        for item in payload.get("items") or []:
            if isinstance(item, dict):
                item = dict(item)
                item["_scout_observed_at"] = started.isoformat() if started else None
                items.append(item)

    age_hours = None
    if newest_started is not None:
        age_hours = (now - newest_started).total_seconds() / 3600
    return items, newest_run_id, age_hours


async def collect_from_scout(
    digest_dir: pathlib.Path,
    store: Store,
    *,
    max_age_hours: int,
    limit: int,
    now: datetime.datetime | None = None,
    include_seen: bool = False,
    prefer_latest_source: bool = False,
) -> ScoutBatch:
    """Collect unseen Scout items, newest and highest-scoring first.

    Seen-state stays in the store at item granularity, which preserves the
    existing interplay: Spark marks only the item it chose, leaving the rest
    eligible for the digest roundup.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    if now.tzinfo is None:
        return ScoutBatch([], None, None, "scout observation time is invalid")
    now = now.astimezone(datetime.UTC)
    # Off the event loop: the files are tiny, but a stalled mount would
    # otherwise block every other brain in the process.
    if not await asyncio.to_thread(digest_dir.exists):
        return ScoutBatch([], None, None, f"scout digest directory missing ({digest_dir})")

    raw, newest_run_id, age_hours = await asyncio.to_thread(_read_digests, digest_dir, now)
    if newest_run_id is None:
        return ScoutBatch([], None, None, "scout has produced no digests")
    if age_hours is not None and age_hours > max_age_hours:
        return ScoutBatch(
            [], newest_run_id, age_hours,
            f"scout output is stale ({age_hours:.0f}h old, limit {max_age_hours}h)",
        )

    # Dedupe across overlapping runs, keeping the highest score seen for an item.
    best: dict[str, dict[str, Any]] = {}
    for item in raw:
        entry = _entry_from_item(item)
        if entry is None:
            continue
        current = best.get(entry["id"])
        entry_observed = _aware_datetime(entry.get("observed_at"))
        current_observed = _aware_datetime(current.get("observed_at")) if current else None
        latest_ok = (
            prefer_latest_source
            and entry["article"].get("status") == "ok"
            and (current is None or current["article"].get("status") != "ok"
                 or (entry_observed is not None and current_observed is not None
                     and entry_observed > current_observed))
        )
        if current is None or latest_ok or (
            not prefer_latest_source and entry["relevance"] > current["relevance"]
        ):
            if current and current["article"].get("status") == "ok" and \
                    entry["article"].get("status") != "ok":
                entry["article"] = current["article"]
                entry["observed_at"] = current["observed_at"]
            best[entry["id"]] = entry
        elif current["article"].get("status") != "ok" and entry["article"].get("status") == "ok":
            current["article"] = entry["article"]
            current["observed_at"] = entry["observed_at"]

    if include_seen:
        entries = list(best.values())
    else:
        by_feed: dict[str, list[str]] = {}
        for entry in best.values():
            by_feed.setdefault(entry["feed_url"], []).append(entry["id"])

        unseen: set[str] = set()
        for feed_url, ids in by_feed.items():
            unseen.update(await store.filter_unseen(feed_url, ids))

        entries = [e for e in best.values() if e["id"] in unseen]
    entries.sort(
        key=lambda e: (e["relevance"], e["published"] or time.gmtime(0)), reverse=True
    )
    return ScoutBatch(entries[:limit], newest_run_id, age_hours, "")
