"""Store — real aiosqlite against a temp DB (integration-level, no mocks)."""

from roger.store import AuditStatus, Store


async def test_record_audit_persists(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        await store.record_audit(
            actor_id=42,
            brain="admin",
            tool=None,
            args={"request": "make a channel"},
            status=AuditStatus.GATE_REJECTED,
            detail="non-owner",
        )
        rows = await store.fetch_audit()
        assert len(rows) == 1
        assert rows[0]["actor_id"] == 42
        assert rows[0]["status"] == "gate_rejected"
        assert "make a channel" in rows[0]["args_json"]
    finally:
        await store.close()


async def test_wal_mode_enabled(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        assert (await store.journal_mode()).lower() == "wal"
    finally:
        await store.close()


async def test_open_creates_missing_parent_dirs(tmp_path):
    nested = tmp_path / "a" / "b" / "roger.db"
    store = await Store(str(nested)).open()
    try:
        assert nested.exists()
    finally:
        await store.close()


async def test_feed_crud_and_dedupe(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        assert await store.count_feeds() == 0
        assert await store.add_feed("http://a", "A") is True
        assert await store.add_feed("http://a", "A") is False  # duplicate URL ignored
        assert await store.add_feed("http://b", None) is True
        assert [f["url"] for f in await store.list_feeds()] == ["http://a", "http://b"]

        assert await store.remove_feed("http://a") is True
        assert await store.remove_feed("http://a") is False  # already gone
        assert [f["url"] for f in await store.list_feeds()] == ["http://b"]
    finally:
        await store.close()


async def test_seed_feeds_ignores_existing(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        await store.add_feed("http://a", None)
        await store.seed_feeds(["http://a", "http://b"])  # "http://a" already present
        assert {f["url"] for f in await store.list_feeds()} == {"http://a", "http://b"}
    finally:
        await store.close()
