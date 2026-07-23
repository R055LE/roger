"""Store — real aiosqlite against a temp DB (integration-level, no mocks)."""

import time

import aiosqlite

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


async def test_usage_accumulates_tokens_and_cost(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        await store.add_usage("admin", 100, 50, cost_usd=0.01)
        await store.add_usage("admin", 10, 5, cost_usd=0.002)  # same day+brain -> summed
        assert await store.usage_today("admin") == 165
        assert abs(await store.cost_today("admin") - 0.012) < 1e-9
        # cost defaults to 0 and is isolated per brain
        await store.add_usage("ambient", 3, 2)
        assert await store.cost_today("ambient") == 0.0
    finally:
        await store.close()


async def test_migration_backfills_cost_column_on_preexisting_db(tmp_path):
    """A DB provisioned before cost_usd existed must gain the column without losing rows."""
    path = str(tmp_path / "old.db")
    # Hand-build the pre-cost `usage` table and a row, mimicking an already-deployed DB.
    raw = await aiosqlite.connect(path)
    await raw.execute(
        "CREATE TABLE usage (date TEXT NOT NULL, brain TEXT NOT NULL, "
        "tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (date, brain))"
    )
    await raw.execute(
        "INSERT INTO usage (date, brain, tokens_in, tokens_out) VALUES (?, 'admin', 100, 50)",
        (time.strftime("%Y-%m-%d"),),
    )
    await raw.commit()
    await raw.close()

    store = await Store(path).open()  # runs _migrate()
    try:
        assert await store.usage_today("admin") == 150  # existing row survived
        assert await store.cost_today("admin") == 0.0  # column backfilled to the default
        await store.add_usage("admin", 0, 0, cost_usd=0.005)
        assert abs(await store.cost_today("admin") - 0.005) < 1e-9
        await store.close()
        # Reopening an already-current DB must be a harmless no-op (idempotent migration).
        store = await Store(path).open()
        assert abs(await store.cost_today("admin") - 0.005) < 1e-9
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
