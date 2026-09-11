"""Read Scout's digest output as the item source for the scheduled brains (§9).

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

Availability. Scout is a hard dependency of the scheduled brains. When its
output is missing or stale the brains report that rather than silently posting
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
# post, a restart, a brain that did not fire) would be lost for good. The store's
# seen table does the deduplication, so overlapping windows are free.
WINDOW_HOURS = 72
MAX_FILES = 32
_SUMMARY_CAP = 500  # matches the digest brain's own cap


@dataclasses.dataclass(frozen=True)
class ScoutBatch:
    entries: list[dict[str, Any]]
    newest_run_id: str | None
    age_hours: float | None
    status: str  # "" when usable, else a reason suitable for a job status


def _to_struct_time(value: object) -> time.struct_time | None:
    """Scout emits ISO-8601; the brains sort on ``time.struct_time`` like feedparser."""
    try:
        return datetime.datetime.fromisoformat(str(value)).timetuple()
    except (TypeError, ValueError):
        return None


def _entry_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """Map one Scout item onto the dict shape the brains already consume.

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
        # Carried through so the model is told why an item surfaced. This is the
        # whole point of consuming a scored source instead of a raw feed.
        "relevance": int(item.get("relevance") or 0),
        "matched": item.get("matched") or [],
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
        started = None
        try:
            started = datetime.datetime.fromisoformat(payload["run"]["started_at"])
        except (KeyError, TypeError, ValueError):
            pass
        if started is not None:
            if newest_started is None:
                newest_started = started
            if started < cutoff:
                break
        items.extend(payload.get("items") or [])

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
) -> ScoutBatch:
    """Collect unseen Scout items, newest and highest-scoring first.

    Seen-state stays in the store at item granularity, which preserves the
    existing interplay: Spark marks only the item it chose, leaving the rest
    eligible for the digest roundup.
    """
    now = now or datetime.datetime.now(datetime.UTC)
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
        if current is None or entry["relevance"] > current["relevance"]:
            best[entry["id"]] = entry

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
