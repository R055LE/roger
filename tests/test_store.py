"""Store — real aiosqlite against a temp DB (integration-level, no mocks)."""

import asyncio
import time

import aiosqlite
import pytest

from roger.request_context import request_context
from roger.store import CURATED_CHECK_LEASE_SECONDS, RETENTION_DAYS, AuditStatus, Store


def _attempt(workflow_id="workflow-a", **overrides):
    args = dict(
        workflow_id=workflow_id, role="writer", profile="sol", input_tokens=100,
        output_tokens=200, cost_usd="0.01", token_cap=1000, usd_cap="0.1",
        max_attempts=10, max_workflows=10, timeout_seconds=1200, now=1000,
    )
    args.update(overrides)
    return args


@pytest.mark.parametrize("shared_connection", [False, True])
@pytest.mark.parametrize("limit", [
    {"token_cap": 800}, {"usd_cap": "0.02"}, {"max_attempts": 1}, {"max_workflows": 1},
])
async def test_curated_reservations_cannot_spend_the_same_last_budget(
    tmp_path, shared_connection, limit,
):
    path = str(tmp_path / "roger.db")
    first = await Store(path).open()
    second = first if shared_connection else await Store(path).open()
    try:
        await first.add_usage("curated", 400, 100, 0.01)
        claims = await asyncio.gather(
            first.reserve_curated_attempt(**_attempt("a", **limit)),
            second.reserve_curated_attempt(**_attempt("b", **limit)),
            first.add_usage("admin", 10, 20, 0.1),
        )
        assert sum(claim is not None for claim in claims[:2]) == 1
        state = await first.curated_budget_state(now=1001)
        assert state["used_tokens"] == 500
        assert state["reserved_tokens"] == 300
        assert state["attempts_today"] == state["workflows_today"] == 1
    finally:
        if second is not first:
            await second.close()
        await first.close()


@pytest.mark.parametrize("settlement", [False, True])
async def test_curated_ledger_survives_other_store_methods_rolling_back(
    tmp_path, monkeypatch, settlement,
):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        claim = await store.reserve_curated_attempt(**_attempt()) if settlement else None
        await store._conn.execute("BEGIN")
        original = store._conn.execute_fetchall

        async def interleave_rollback(sql, parameters=None):
            rows = await original(sql, parameters)
            if "RETURNING" in sql:
                with pytest.raises(RuntimeError, match="claim missing"):
                    await store.mark_curated_sent(-1, 1)
            return rows

        monkeypatch.setattr(store._conn, "execute_fetchall", interleave_rollback)
        if settlement:
            assert await store.finish_curated_attempt(
                claim, input_tokens=10, output_tokens=20, cost_usd="0.001",
            ) == "settled"
        else:
            claim = await store.reserve_curated_attempt(**_attempt())
            assert claim is not None
        with pytest.raises(RuntimeError, match="claim missing"):
            await store.mark_curated_sent(-1, 1)
        rows = await original("SELECT state FROM curated_model_attempt WHERE id = ?", (claim,))
        assert [row[0] for row in rows] == ["settled" if settlement else "reserved"]
    finally:
        await store.close()


async def test_curated_memory_store_shares_usage_and_ledger():
    store = await Store(":memory:").open()
    try:
        await store.add_usage("curated", 100, 200, 0.01)
        assert await store.reserve_curated_attempt(**_attempt(token_cap=599)) is None
        claim = await store.reserve_curated_attempt(**_attempt(token_cap=600))
        assert claim is not None
        assert (await store.curated_budget_state(now=1001))["reserved_tokens"] == 300
        assert await store.finish_curated_attempt(
            claim, input_tokens=10, output_tokens=20, cost_usd="0.001",
        ) == "settled"
        assert await store.usage_today("curated") == 330
    finally:
        await store.close()


async def test_curated_status_read_cannot_hold_a_snapshot_across_admission(tmp_path, monkeypatch):
    path = str(tmp_path / "roger.db")
    first, second = await Store(path).open(), await Store(path).open()
    read_started, release_read = asyncio.Event(), asyncio.Event()
    original_execute = first._curated_conn.execute
    original_fetchall = first._curated_conn.execute_fetchall

    async def execute(sql, parameters=None):
        cursor = await original_execute(sql, parameters)
        if "AS denial_reason FROM budget" in sql:
            read_started.set()
            await release_read.wait()
        return cursor

    async def execute_fetchall(sql, parameters=None):
        rows = await original_fetchall(sql, parameters)
        if "AS denial_reason FROM budget" in sql:
            read_started.set()
            await release_read.wait()
        return rows

    monkeypatch.setattr(first._curated_conn, "execute", execute)
    monkeypatch.setattr(first._curated_conn, "execute_fetchall", execute_fetchall)
    status = asyncio.create_task(first.curated_budget_state(now=1001))
    try:
        await asyncio.wait_for(read_started.wait(), timeout=5)
        assert await second.reserve_curated_attempt(**_attempt("other-connection")) is not None
        assert await first.reserve_curated_attempt(**_attempt("status-connection")) is not None
    finally:
        release_read.set()
        await status
        await first.close()
        await second.close()


@pytest.mark.parametrize(("limit", "reason"), [
    ({"token_cap": 299}, "tokens"), ({"usd_cap": "0.009999999999"}, "usd"),
    ({"max_attempts": 0}, "disabled"), ({"max_workflows": 0}, "disabled"),
    ({"token_cap": 0}, "disabled"), ({"usd_cap": 0}, "disabled"),
])
async def test_curated_admission_reports_the_current_gate(tmp_path, limit, reason):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        request = _attempt(**limit)
        assert await store.reserve_curated_attempt(**request) is None
        state = await store.curated_budget_state(request=request)
        assert state["denial_reason"] == reason
        assert state["attempts_today"] == 0
    finally:
        await store.close()


async def test_curated_workflow_and_attempt_limits_are_independent(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        for _ in range(4):
            claim = await store.reserve_curated_attempt(**_attempt(max_workflows=1))
            assert claim is not None
            assert await store.finish_curated_attempt(
                claim, input_tokens=10, output_tokens=20, cost_usd="0.001",
            ) == "settled"
        request = _attempt(max_workflows=1)
        assert await store.reserve_curated_attempt(**request) is None
        state = await store.curated_budget_state(request=request)
        assert state["denial_reason"] == "workflow_attempts"
        assert state["attempts_today"] == 4
        assert state["workflows_today"] == 1
        request = _attempt("new-workflow", max_workflows=1)
        assert await store.reserve_curated_attempt(**request) is None
        assert (await store.curated_budget_state(request=request))["denial_reason"] == (
            "daily_workflows"
        )
        request = _attempt("new-workflow", max_attempts=4)
        assert await store.reserve_curated_attempt(**request) is None
        assert (await store.curated_budget_state(request=request))["denial_reason"] == (
            "daily_attempts"
        )
    finally:
        await store.close()


@pytest.mark.parametrize("shared_connection", [False, True])
async def test_curated_settlement_is_exactly_once_and_adds_legacy_usage(
    tmp_path, shared_connection,
):
    path = str(tmp_path / "roger.db")
    first = await Store(path).open()
    second = first if shared_connection else await Store(path).open()
    try:
        await first.add_usage("curated", 30, 20, 0.005)
        claim = await first.reserve_curated_attempt(**_attempt())
        results = await asyncio.gather(*[
            store.finish_curated_attempt(
                claim, input_tokens=10, output_tokens=20, cost_usd="0.00123456789",
            ) for store in (first, second)
        ])
        assert sorted(str(result) for result in results) == ["None", "settled"]
        assert await first.usage_today("curated") == 80
        assert await first.cost_today("curated") == pytest.approx(0.00623456789)
        state = await first.curated_budget_state(now=1001)
        assert state["reserved_tokens"] == state["unknown_tokens"] == 0
        assert state["used_cost_units"] == 6_234_568
        assert await first.mark_curated_attempt_uncertain(claim) is None
        assert await first.usage_today("admin") == 0
    finally:
        if second is not first:
            await second.close()
        await first.close()


@pytest.mark.parametrize("usage", [
    {}, {"input_tokens": -1, "output_tokens": 10, "cost_usd": "0.001"},
    {"input_tokens": True, "output_tokens": 10, "cost_usd": "0.001"},
    {"input_tokens": 10, "output_tokens": 1.5, "cost_usd": "0.001"},
    {"input_tokens": 10, "output_tokens": 20, "cost_usd": float("nan")},
    {"input_tokens": 10, "output_tokens": 20, "cost_usd": float("inf")},
    {"input_tokens": 10, "output_tokens": 20, "cost_usd": -0.001},
    {"input_tokens": 10, "output_tokens": 20, "cost_usd": "unusable"},
    {"input_tokens": 10, "output_tokens": 20, "cost_usd": "0.001", "uncertain": True},
])
async def test_curated_untrustworthy_outcomes_keep_full_reservation(tmp_path, usage):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        claim = await store.reserve_curated_attempt(**_attempt())
        assert await store.finish_curated_attempt(claim, **usage) == "uncertain"
        assert await store.finish_curated_attempt(
            claim, input_tokens=1, output_tokens=1, cost_usd=0,
        ) is None
        state = await store.curated_budget_state(now=1001)
        assert state["used_tokens"] == state["reserved_tokens"] == 0
        assert state["unknown_tokens"] == 300
        assert state["unknown_usd"] == 0.01
        assert state["unknown_attempts"] == 1
        assert await store.reserve_curated_attempt(**_attempt("new", token_cap=599)) is None
    finally:
        await store.close()


@pytest.mark.parametrize("usage", [
    {"input_tokens": 150, "output_tokens": 250, "cost_usd": "0.02"},
    {"cost_usd": "0.02"},
])
async def test_curated_known_overrun_never_shrinks_to_reserved_charge(tmp_path, usage):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        claim = await store.reserve_curated_attempt(**_attempt())
        assert await store.finish_curated_attempt(claim, **usage) == "overrun"
        state = await store.curated_budget_state(now=1001)
        assert state["unknown_tokens"] == (400 if "input_tokens" in usage else 300)
        assert state["unknown_usd"] == 0.02
        assert await store.reserve_curated_attempt(**_attempt("new", usd_cap="0.029")) is None
        rows = await store._conn.execute_fetchall(
            "SELECT actual_cost_usd FROM curated_model_attempt WHERE id = ?", (claim,),
        )
        assert rows[0][0] == "0.02"
    finally:
        await store.close()


async def test_curated_overrun_blocks_new_workflows_after_midnight_and_cap_increase(
    tmp_path, monkeypatch,
):
    path = str(tmp_path / "roger.db")
    monkeypatch.setattr("roger.store._today", lambda: "2026-10-01")
    store = await Store(path).open()
    claim = await store.reserve_curated_attempt(**_attempt())
    assert await store.finish_curated_attempt(
        claim, input_tokens=10, output_tokens=20, cost_usd="0.02",
    ) == "overrun"
    await store.close()
    monkeypatch.setattr("roger.store._today", lambda: "2026-10-02")
    store = await Store(path).open()
    try:
        request = _attempt("new-workflow", token_cap=100_000, usd_cap="10")
        assert await store.reserve_curated_attempt(**request) is None
        state = await store.curated_budget_state(request=request)
        assert state["denial_reason"] == "reservation_overrun"
        assert state["overrun_attempts"] == 1
        assert state["attempts_today"] == 0
        assert state["unknown_usd"] == 0.02
    finally:
        await store.close()


async def test_curated_unknowns_survive_restart_midnight_and_prune(tmp_path, monkeypatch):
    path = str(tmp_path / "roger.db")
    monkeypatch.setattr("roger.store._today", lambda: "2026-10-01")
    store = await Store(path).open()
    claim = await store.reserve_curated_attempt(**_attempt("carried"))
    pending = await store.reserve_curated_attempt(**_attempt("unknown"))
    await store.close()
    monkeypatch.setattr("roger.store._today", lambda: "2026-10-02")
    store = await Store(path).open()
    try:
        state = await store.curated_budget_state(now=1100)
        assert state["attempts_today"] == state["workflows_today"] == 0
        assert state["reserved_tokens"] == 600
        assert state["unknown_tokens"] == 0
        state = await store.curated_budget_state(now=2200)
        assert state["reserved_tokens"] == 0
        assert state["unknown_tokens"] == 600
        assert await store.reserve_curated_attempt(**_attempt("new", token_cap=899)) is None
        assert await store.finish_curated_attempt(
            claim, input_tokens=10, output_tokens=20, cost_usd="0.001", now=2201,
        ) == "settled"
        assert await store.usage_today("curated") == 30
        assert await store.cost_today("curated") == 0.001
        assert await store.reserve_curated_attempt(**_attempt("carried", now=2202)) is not None
        state = await store.curated_budget_state(now=2202)
        assert state["attempts_today"] == state["workflows_today"] == 1
        assert state["unknown_tokens"] == 300
        await store.prune(now=time.time() + 400 * 86400)
        assert (await store.curated_budget_state(now=2202))["unknown_tokens"] == 300
        assert await store.mark_curated_attempt_uncertain(pending) == "uncertain"
    finally:
        await store.close()


async def test_curated_fractional_nanocharges_round_against_admission(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        await store.add_usage("curated", 0, 0, 0.0000000001)
        request = _attempt(cost_usd="0.0000000001", usd_cap="0.0000000019")
        assert await store.reserve_curated_attempt(**request) is None
        assert (await store.curated_budget_state(request=request))["denial_reason"] == "usd"
        request["usd_cap"] = "0.000000002"
        assert await store.reserve_curated_attempt(**request) is not None
    finally:
        await store.close()


@pytest.mark.parametrize("invalid", [
    {"workflow_id": ""}, {"workflow_id": "secret/request"}, {"profile": "x" * 81},
    {"role": "admin"}, {"input_tokens": 32_769}, {"output_tokens": 16_385},
    {"input_tokens": True}, {"max_attempts": -1}, {"cost_usd": "NaN"},
    {"usd_cap": -1}, {"timeout_seconds": 0}, {"now": float("inf")},
])
async def test_curated_invalid_reservation_cannot_create_a_ledger_row(tmp_path, invalid):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        with pytest.raises(ValueError):
            await store.reserve_curated_attempt(**_attempt(**invalid))
        assert (await store.curated_budget_state())["attempts_today"] == 0
    finally:
        await store.close()


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

