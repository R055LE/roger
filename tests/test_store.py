"""Store — real aiosqlite against a temp DB (integration-level, no mocks)."""

import asyncio
import time

import aiosqlite
import pytest

from roger.request_context import request_context
from roger.store import CURATED_CHECK_LEASE_SECONDS, RETENTION_DAYS, AuditStatus, Store


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
        assert rows[0]["request_id"] is None
    finally:
        await store.close()


async def test_curated_migration_preserves_every_legacy_delivery_and_seen_key(tmp_path):
    path = str(tmp_path / "legacy.db")
    raw = await aiosqlite.connect(path)
    await raw.execute(
        "CREATE TABLE curated_delivery (local_date TEXT PRIMARY KEY, feed_url TEXT NOT NULL, "
        "entry_id TEXT NOT NULL, status TEXT NOT NULL, message_id TEXT, ts REAL NOT NULL)"
    )
    await raw.executemany(
        "INSERT INTO curated_delivery VALUES (?, 'feed', 'same-item', ?, ?, ?)",
        [("2026-09-30", "sent", "123", 100.0), ("2026-10-01", "pending", None, 200.0)],
    )
    await raw.execute(
        "CREATE TABLE seen (feed_url TEXT, entry_id TEXT, ts REAL, PRIMARY KEY(feed_url, entry_id))"
    )
    await raw.execute("INSERT INTO seen VALUES ('feed', 'same-item', 100)")
    await raw.commit()
    await raw.close()
    for _ in range(2):
        store = await Store(path).open()
        try:
            sent = await store.curated_delivery("2026-09-30")
            pending = await store.curated_delivery("2026-10-01")
            assert (sent["status"], sent["message_id"], sent["ts"]) == ("sent", "123", 100)
            assert (pending["status"], pending["message_id"], pending["ts"]) == (
                "pending", None, 200,
            )
            assert sent["id"] != pending["id"]
            assert await store.filter_unseen("feed", ["same-item", "new-item"]) == {"new-item"}
            state = await store.curated_delivery_state("2026-10-01")
            assert state["used"] == state["pending_deliveries"] == 1
            assert state["last_claim_at"] == 200
        finally:
            await store.close()


async def test_concurrent_curated_claims_cannot_take_the_same_last_slot(tmp_path):
    path = str(tmp_path / "roger.db")
    first, second = await Store(path).open(), await Store(path).open()
    try:
        generation = await first.claim_curated_check(1000)
        claims = await asyncio.gather(*[
            store.claim_curated("2026-10-01", "feed", entry_id, event_key=entry_id,
                                max_posts=1, spacing_seconds=0, generation=generation, now=1000)
            for store, entry_id in [(first, "a"), (second, "b")]
        ])
        assert sum(claim is not None for claim in claims) == 1
        assert (await first.curated_delivery_state("2026-10-01"))["used"] == 1
        assert len(await first.filter_unseen("feed", ["a", "b"])) == 1
    finally:
        await first.close()
        await second.close()


async def test_curated_item_and_event_claims_survive_restart_and_new_day(tmp_path):
    path = str(tmp_path / "roger.db")
    store = await Store(path).open()
    try:
        generation = await store.claim_curated_check(1000)
        claim = await store.claim_curated(
            "2026-10-01", "feed", "a", event_key="https://example.org/event",
            max_posts=3, spacing_seconds=0, generation=generation, now=1000,
        )
        await store.mark_curated_sent(claim, 123)
        await store.release_curated_check(generation)
    finally:
        await store.close()
    store = await Store(path).open()
    try:
        generation = await store.claim_curated_check(90000)
        for feed, item, event in [("other-feed", "b", "https://example.org/event"),
                                  ("feed", "a", "https://example.org/other")]:
            assert await store.claim_curated(
                "2026-10-02", feed, item, event_key=event, max_posts=3,
                spacing_seconds=0, generation=generation, now=90000,
            ) is None
        assert (await store.curated_delivery_state("2026-10-02"))["used"] == 0
        assert await store.filter_unseen("other-feed", ["b"]) == {"b"}
    finally:
        await store.close()


async def test_expired_curated_worker_cannot_send_or_overwrite_its_successor(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        first = await store.claim_curated_check(1000)
        assert await store.begin_curated_observation(
            ["input"], first, 1000, local_date="2026-10-01", max_observations=8,
        )
        later = 1000 + CURATED_CHECK_LEASE_SECONDS + 1
        second = await store.claim_curated_check(later)
        assert second != first
        await store.finish_curated_observation([("input", "rejected")], first, later)
        await store.release_curated_check(first)
        assert (await store.curated_check_state())["expires_at"] > later
        assert (await store.curated_observations(["input"]))["input"]["status"] == "processing"
        assert await store.claim_curated(
            "2026-10-01", "feed", "item", event_key="event", max_posts=3,
            spacing_seconds=0, generation=first, now=later,
        ) is None
    finally:
        await store.close()


async def test_curated_daily_observation_admission_is_durable_and_resets_on_local_date(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        first = await store.claim_curated_check(1000)
        assert await store.begin_curated_observation(
            ["a"], first, 1000, local_date="2026-10-01", max_observations=1,
        )
        await store.release_curated_check(first)
        second = await store.claim_curated_check(1001)
        assert not await store.begin_curated_observation(
            ["b"], second, 1001, local_date="2026-10-01", max_observations=1,
        )
        assert await store.curated_observations(["b"]) == {}
        await store.release_curated_check(second)
        third = await store.claim_curated_check(1002)
        assert await store.begin_curated_observation(
            ["b"], third, 1002, local_date="2026-10-02", max_observations=1,
        )
        assert (await store.curated_check_state())["observations"] == 1
    finally:
        await store.close()


async def test_curated_claim_and_seen_state_are_atomic_during_another_commit(tmp_path, monkeypatch):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        generation = await store.claim_curated_check(1000)
        await store._conn.execute(
            "CREATE TEMP TRIGGER fail_seen BEFORE INSERT ON seen "
            "BEGIN SELECT RAISE(ABORT, 'seen write failed'); END"
        )
        execute = store._conn.execute

        async def interleaving_execute(sql, parameters=()):
            cursor = await execute(sql, parameters)
            if sql.startswith("INSERT OR IGNORE INTO curated_delivery"):
                await store.set_meta("concurrent_commit", "another brain committed")
            return cursor

        monkeypatch.setattr(store._conn, "execute", interleaving_execute)
        with pytest.raises(aiosqlite.IntegrityError, match="seen write failed"):
            await store.claim_curated(
                "2026-10-01", "feed", "item", event_key="event", max_posts=3,
                spacing_seconds=0, generation=generation, now=1000,
            )
        assert (await store.curated_delivery_state("2026-10-01"))["used"] == 0
        assert await store.filter_unseen("feed", ["item"]) == {"item"}
    finally:
        await store.close()


async def test_curated_legacy_migration_rolls_back_a_failed_copy(tmp_path, monkeypatch):
    path = str(tmp_path / "legacy.db")
    raw = await aiosqlite.connect(path)
    await raw.execute(
        "CREATE TABLE curated_delivery (local_date TEXT PRIMARY KEY, feed_url TEXT NOT NULL, "
        "entry_id TEXT NOT NULL, status TEXT NOT NULL, message_id TEXT, ts REAL NOT NULL)"
    )
    await raw.execute(
        "INSERT INTO curated_delivery VALUES ('2026-10-01', 'feed', 'item', 'sent', '123', 1000)"
    )
    await raw.commit()
    await raw.close()
    execute = aiosqlite.Connection.execute

    async def fail_copy(self, sql, parameters=()):
        if sql.startswith("INSERT INTO curated_delivery "):
            raise RuntimeError("copy interrupted")
        return await execute(self, sql, parameters)

    broken = Store(path)
    monkeypatch.setattr(aiosqlite.Connection, "execute", fail_copy)
    try:
        with pytest.raises(RuntimeError, match="copy interrupted"):
            await broken.open()
    finally:
        await broken.close()
    monkeypatch.setattr(aiosqlite.Connection, "execute", execute)
    recovered = await Store(path).open()
    try:
        row = await recovered.curated_delivery("2026-10-01")
        assert row["status"] == "sent" and row["message_id"] == "123"
        assert row["ts"] == 1000
    finally:
        await recovered.close()


async def test_meta_roundtrip_and_upsert(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        assert await store.get_meta("presence") is None  # unset reads as None
        await store.set_meta("presence", '{"status": "idle"}')
        assert await store.get_meta("presence") == '{"status": "idle"}'
        await store.set_meta("presence", '{"status": "dnd"}')  # upsert, not a second row
        assert await store.get_meta("presence") == '{"status": "dnd"}'
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


async def test_migration_adds_request_id_to_preexisting_audit_without_losing_rows(tmp_path):
    path = str(tmp_path / "old.db")
    raw = await aiosqlite.connect(path)
    await raw.execute(
        "CREATE TABLE audit (id INTEGER PRIMARY KEY, ts REAL NOT NULL, actor_id INTEGER, "
        "brain TEXT, tool TEXT, args_json TEXT, status TEXT NOT NULL, detail TEXT)"
    )
    await raw.execute(
        "INSERT INTO audit (ts, actor_id, status) VALUES (?, ?, ?)", (time.time(), 42, "ok")
    )
    await raw.commit()
    await raw.close()

    store = await Store(path).open()
    try:
        rows = await store.fetch_audit()
        assert len(rows) == 1
        assert rows[0]["actor_id"] == 42
        assert rows[0]["request_id"] is None
        await store.close()
        store = await Store(path).open()
        assert (await store.fetch_audit())[0]["request_id"] is None
    finally:
        await store.close()


async def test_record_audit_uses_bound_request_id(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        with request_context() as request_id:
            await store.record_audit(
                actor_id=42,
                brain="admin",
                tool=None,
                args=None,
                status=AuditStatus.OK,
            )
        assert (await store.fetch_audit())[0]["request_id"] == request_id
    finally:
        await store.close()


async def test_prune_drops_expired_rows_and_keeps_recent(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        now = time.time()
        old = now - 100 * 86_400  # 100 days ago — past ambient(30)/admin(30)/seen(90) windows
        recent = now - 1 * 86_400  # yesterday — inside every window

        # ambient_log / admin_log go through the public writers, then we backdate one row each.
        await store.add_ambient(1, 2, "user", "old")
        await store.add_ambient(1, 2, "user", "fresh")
        conn = store._conn
        await conn.execute("UPDATE ambient_log SET ts = ? WHERE content = 'old'", (old,))
        await conn.execute("UPDATE ambient_log SET ts = ? WHERE content = 'fresh'", (recent,))
        await store.mark_seen([("http://f", "old"), ("http://f", "fresh")])
        await conn.execute("UPDATE seen SET ts = ? WHERE entry_id = 'old'", (old,))
        await conn.execute("UPDATE seen SET ts = ? WHERE entry_id = 'fresh'", (recent,))
        await conn.commit()

        deleted = await store.prune(now=now)
        assert deleted["ambient_log"] == 1 and deleted["seen"] == 1

        rows = await store.recent_ambient(1, 2)
        assert [r["content"] for r in rows] == ["fresh"]  # only the recent row survives
        # 'old' is unseen again (its dedupe row pruned); 'fresh' is still marked seen
        assert await store.filter_unseen("http://f", ["old", "fresh"]) == {"old"}

        # Idempotent: a second pass finds nothing left to remove.
        assert await store.prune(now=now) == dict.fromkeys(RETENTION_DAYS, 0)
    finally:
        await store.close()

