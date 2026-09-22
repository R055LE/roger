"""Scout source — reading digests, the staleness gate, dedupe and seen-filtering."""

import datetime
import json

from conftest import write_digest as _write_digest

from roger import scout_source
from roger.scout_source import collect_from_scout
from roger.store import Store


def _entry(entry_id, title="t", link=None, summary="s", published=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        id=entry_id,
        title=title,
        link=link if link is not None else f"https://example.org/{entry_id}",
        summary=summary,
        published_parsed=published,
    )


async def _store(tmp_path):
    return await Store(str(tmp_path / "s.db")).open()


async def _collect(tmp_path, store, *, max_age_hours=36, limit=50):
    return await collect_from_scout(
        tmp_path / "digests", store, max_age_hours=max_age_hours, limit=limit
    )


async def test_reads_items_and_carries_the_reason(tmp_path):
    store = await _store(tmp_path)
    try:
        _write_digest(tmp_path, [_entry("a"), _entry("b")])
        batch = await _collect(tmp_path, store)
        assert batch.status == ""
        assert {e["id"] for e in batch.entries} == {"a", "b"}
        # The reason is the point of consuming a scored source.
        assert all(e["matched"] for e in batch.entries)
        assert all(e["relevance"] == 3 for e in batch.entries)
    finally:
        await store.close()


async def test_missing_directory_reports_rather_than_returning_empty(tmp_path):
    store = await _store(tmp_path)
    try:
        batch = await _collect(tmp_path, store)
        assert "missing" in batch.status
        assert batch.entries == []
    finally:
        await store.close()


async def test_empty_directory_reports_no_digests(tmp_path):
    store = await _store(tmp_path)
    try:
        (tmp_path / "digests").mkdir()
        batch = await _collect(tmp_path, store)
        assert "no digests" in batch.status
    finally:
        await store.close()


async def test_stale_output_is_a_status_not_a_quiet_day(tmp_path):
    """A broken producer must not read as 'nothing happened today'."""
    store = await _store(tmp_path)
    try:
        _write_digest(tmp_path, [_entry("a")], age_hours=50)
        batch = await _collect(tmp_path, store, max_age_hours=36)
        assert "stale" in batch.status
        assert batch.entries == []
        assert batch.age_hours is not None and batch.age_hours > 36
    finally:
        await store.close()


async def test_reads_a_window_not_just_the_newest_run(tmp_path):
    """Scout never re-reports an item, so a missed run would be lost forever."""
    store = await _store(tmp_path)
    try:
        _write_digest(tmp_path, [_entry("old")], run_id="2026-09-01T00:00:00Z-a", age_hours=10)
        _write_digest(tmp_path, [_entry("new")], run_id="2026-09-02T00:00:00Z-b", age_hours=1)
        batch = await _collect(tmp_path, store)
        assert {e["id"] for e in batch.entries} == {"old", "new"}
    finally:
        await store.close()


async def test_runs_outside_the_window_are_dropped(tmp_path):
    store = await _store(tmp_path)
    try:
        _write_digest(tmp_path, [_entry("ancient")], run_id="2026-01-01T00:00:00Z-a",
                      age_hours=scout_source.WINDOW_HOURS + 24)
        _write_digest(tmp_path, [_entry("fresh")], run_id="2026-09-02T00:00:00Z-b", age_hours=1)
        batch = await _collect(tmp_path, store)
        assert [e["id"] for e in batch.entries] == ["fresh"]
    finally:
        await store.close()


async def test_seen_items_are_filtered(tmp_path):
    store = await _store(tmp_path)
    try:
        _write_digest(tmp_path, [_entry("a"), _entry("b")])
        await store.mark_seen([("scout:f", "a")])
        batch = await _collect(tmp_path, store)
        assert [e["id"] for e in batch.entries] == ["b"]
    finally:
        await store.close()


async def test_duplicate_across_runs_keeps_the_higher_score(tmp_path):
    store = await _store(tmp_path)
    try:
        _write_digest(tmp_path, [_entry("dup")], run_id="2026-09-01T00:00:00Z-a", age_hours=5)
        path = tmp_path / "digests" / "2026-09-02T00:00:00Z-b.json"
        started = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
        path.write_text(json.dumps({
            "run": {"run_id": "2026-09-02T00:00:00Z-b", "started_at": started.isoformat()},
            "items": [{"native_id": "dup", "url": "https://example.org/dup", "title": "t",
                       "summary": "s", "published": started.isoformat(), "relevance": 9,
                       "matched": [], "extra": {"feed": "f"}, "source": "rss"}],
        }))
        batch = await _collect(tmp_path, store)
        assert len(batch.entries) == 1
        assert batch.entries[0]["relevance"] == 9
    finally:
        await store.close()


async def test_a_damaged_digest_does_not_cost_the_readable_ones(tmp_path):
    store = await _store(tmp_path)
    try:
        _write_digest(tmp_path, [_entry("good")], run_id="2026-09-02T00:00:00Z-b", age_hours=1)
        (tmp_path / "digests" / "2026-09-01T00:00:00Z-a.json").write_text("{ truncated")
        batch = await _collect(tmp_path, store)
        assert [e["id"] for e in batch.entries] == ["good"]
    finally:
        await store.close()


async def test_sorted_by_relevance_then_recency_and_capped(tmp_path):
    store = await _store(tmp_path)
    try:
        started = datetime.datetime.now(datetime.UTC)
        path = tmp_path / "digests"
        path.mkdir()
        (path / "2026-09-02T00:00:00Z-b.json").write_text(json.dumps({
            "run": {"run_id": "2026-09-02T00:00:00Z-b", "started_at": started.isoformat()},
            "items": [
                {"native_id": f"i{n}", "url": f"https://example.org/{n}", "title": "t",
                 "summary": "s", "published": started.isoformat(), "relevance": n,
                 "matched": [], "extra": {"feed": "f"}, "source": "rss"}
                for n in (1, 5, 3)
            ],
        }))
        batch = await _collect(tmp_path, store, limit=2)
        assert [e["relevance"] for e in batch.entries] == [5, 3]
    finally:
        await store.close()


async def test_items_without_an_id_or_link_are_skipped(tmp_path):
    store = await _store(tmp_path)
    try:
        started = datetime.datetime.now(datetime.UTC)
        path = tmp_path / "digests"
        path.mkdir()
        (path / "2026-09-02T00:00:00Z-b.json").write_text(json.dumps({
            "run": {"run_id": "2026-09-02T00:00:00Z-b", "started_at": started.isoformat()},
            "items": [
                {"native_id": "", "url": "", "title": "junk"},
                {"native_id": "ok", "url": "https://example.org/ok", "title": "t",
                 "summary": "s", "published": started.isoformat(), "relevance": 3,
                 "matched": [], "extra": {"feed": "f"}, "source": "rss"},
            ],
        }))
        batch = await _collect(tmp_path, store)
        assert [e["id"] for e in batch.entries] == ["ok"]
    finally:
        await store.close()


async def test_unparseable_published_does_not_crash_the_batch(tmp_path):
    store = await _store(tmp_path)
    try:
        started = datetime.datetime.now(datetime.UTC)
        path = tmp_path / "digests"
        path.mkdir()
        (path / "2026-09-02T00:00:00Z-b.json").write_text(json.dumps({
            "run": {"run_id": "2026-09-02T00:00:00Z-b", "started_at": started.isoformat()},
            "items": [{"native_id": "a", "url": "https://example.org/a", "title": "t",
                       "summary": "s", "published": "not-a-date", "relevance": 3,
                       "matched": [], "extra": {"feed": "f"}, "source": "rss"}],
        }))
        batch = await _collect(tmp_path, store)
        assert batch.entries[0]["published"] is None
    finally:
        await store.close()
