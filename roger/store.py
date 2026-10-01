"""Persistence layer (aiosqlite, WAL). One writable file under ``/data``.

The full schema (§10 of the spec) is created up front so later phases add behaviour, not
migrations. P1 uses the ``audit`` table; ``seen`` / ``usage`` / ``ambient_log`` come online with
the digest, budgets, and ambient memory respectively.
"""

from __future__ import annotations

import json
import os
import time
from enum import StrEnum
from typing import Any

import aiosqlite

from roger.request_context import current_request_id


class AuditStatus(StrEnum):
    OK = "ok"
    DENIED = "denied"
    INVALID = "invalid"
    ERROR = "error"
    GATE_REJECTED = "gate_rejected"


_CURATED_DELIVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS curated_delivery (
    id         INTEGER PRIMARY KEY,
    local_date TEXT NOT NULL,
    feed_url   TEXT NOT NULL,
    entry_id   TEXT NOT NULL,
    event_key  TEXT NOT NULL UNIQUE,
    status     TEXT NOT NULL,
    message_id TEXT,
    ts         REAL NOT NULL
)
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY,
    ts        REAL    NOT NULL,
    actor_id  INTEGER,
    brain     TEXT,
    tool      TEXT,
    args_json TEXT,
    status    TEXT    NOT NULL,
    detail    TEXT,
    request_id TEXT
);

CREATE TABLE IF NOT EXISTS seen (
    feed_url TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    ts       REAL NOT NULL,
    PRIMARY KEY (feed_url, entry_id)
);

CREATE TABLE IF NOT EXISTS curated_check (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    generation INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    local_date TEXT NOT NULL DEFAULT '',
    observations INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS curated_observation (
    fingerprint TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    failures    INTEGER NOT NULL,
    retry_at    REAL NOT NULL,
    ts          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    date       TEXT    NOT NULL,
    brain      TEXT    NOT NULL,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd   REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (date, brain)
);

CREATE TABLE IF NOT EXISTS ambient_log (
    id         INTEGER PRIMARY KEY,
    ts         REAL    NOT NULL,
    user_id    INTEGER NOT NULL,
    channel_id INTEGER,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_log (
    id         INTEGER PRIMARY KEY,
    ts         REAL    NOT NULL,
    user_id    INTEGER NOT NULL,
    channel_id INTEGER,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS gigabrain_log (
    id         INTEGER PRIMARY KEY,
    ts         REAL    NOT NULL,
    user_id    INTEGER NOT NULL,
    channel_id INTEGER,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

CURATED_CHECK_LEASE_SECONDS = 30 * 60


def _today() -> str:
    return time.strftime("%Y-%m-%d")


_DAY_SECONDS = 86_400

# Retention windows (days) for the time-series tables. `audit` is the tamper-evident trail, so it's
# kept the longest; ambient/admin conversation memory is short-lived by design (privacy + it stops
# being useful context quickly); `seen` only needs to outlive a feed's practical re-post window.
RETENTION_DAYS: dict[str, int] = {
    "ambient_log": 30,
    "admin_log": 30,
    "gigabrain_log": 30,
    "seen": 90,
    "audit": 365,
    "curated_observation": 7,
}


class Store:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> Store:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(_SCHEMA + _CURATED_DELIVERY_SCHEMA + ";")
        await self._migrate()
        await self._db.commit()
        return self

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store used before open()")
        return self._db

    async def _migrate(self) -> None:
        """Additive, idempotent migrations for columns ``CREATE TABLE IF NOT EXISTS`` can't add.

        A DB provisioned before a column existed skips the current ``CREATE TABLE IF NOT EXISTS``
        schema, so additive column checks keep its data intact and make current DBs a no-op.
        """
        if not await self._has_column("curated_delivery", "id"):
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                if not await self._has_column("curated_delivery", "id"):
                    await self._conn.execute(
                        "ALTER TABLE curated_delivery RENAME TO curated_delivery_daily"
                    )
                    await self._conn.execute(_CURATED_DELIVERY_SCHEMA)
                    await self._conn.execute(
                        "INSERT INTO curated_delivery "
                        "(local_date, feed_url, entry_id, event_key, status, message_id, ts) "
                        "SELECT local_date, feed_url, entry_id, 'legacy:' || local_date, "
                        "status, message_id, ts FROM curated_delivery_daily"
                    )
                    await self._conn.execute("DROP TABLE curated_delivery_daily")
                await self._conn.commit()
            except Exception:
                await self._conn.rollback()
                raise
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS curated_delivery_date ON curated_delivery(local_date)"
        )
        # Other brains commit this shared connection between awaits. A trigger
        # keeps suppression in the claim INSERT even when those commits interleave.
        await self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS curated_delivery_seen AFTER INSERT ON curated_delivery "
            "BEGIN INSERT OR IGNORE INTO seen (feed_url, entry_id, ts) "
            "VALUES (NEW.feed_url, NEW.entry_id, NEW.ts); END"
        )
        if not await self._has_column("usage", "cost_usd"):
            await self._conn.execute(
                "ALTER TABLE usage ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0"
            )
        if not await self._has_column("audit", "request_id"):
            await self._conn.execute("ALTER TABLE audit ADD COLUMN request_id TEXT")

    async def _has_column(self, table: str, column: str) -> bool:
        # PRAGMA can't be parameterized; `table` is an internal literal, never user input.
        cursor = await self._conn.execute(f"PRAGMA table_info({table})")  # noqa: S608
        return any(row[1] == column for row in await cursor.fetchall())

    async def record_audit(
        self,
        *,
        actor_id: int | None,
        brain: str | None,
        tool: str | None,
        args: dict[str, Any] | None,
        status: AuditStatus,
        detail: str | None = None,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO audit (ts, actor_id, brain, tool, args_json, status, detail, request_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                actor_id,
                brain,
                tool,
                json.dumps(args, default=str) if args is not None else None,
                str(status),
                detail,
                current_request_id(),
            ),
        )
        await self._conn.commit()

    async def fetch_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def audit_tally(self) -> list[dict[str, Any]]:
        """Audit rows grouped by (tool, status) — feeds the `roger_audit_events` metric."""
        cursor = await self._conn.execute(
            "SELECT tool, status, COUNT(*) AS count FROM audit GROUP BY tool, status"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def journal_mode(self) -> str:
        cursor = await self._conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        return str(row[0]) if row else ""

    async def usage_today(self, brain: str) -> int:
        """Total (in + out) tokens recorded for ``brain`` today. Drives the budget gate."""
        cursor = await self._conn.execute(
            "SELECT tokens_in + tokens_out FROM usage WHERE date = ? AND brain = ?",
            (_today(), brain),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    async def add_usage(
        self, brain: str, tokens_in: int, tokens_out: int, cost_usd: float = 0.0
    ) -> None:
        await self._conn.execute(
            "INSERT INTO usage (date, brain, tokens_in, tokens_out, cost_usd) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(date, brain) DO UPDATE SET "
            "tokens_in = tokens_in + excluded.tokens_in, "
            "tokens_out = tokens_out + excluded.tokens_out, "
            "cost_usd = cost_usd + excluded.cost_usd",
            (_today(), brain, tokens_in, tokens_out, cost_usd),
        )
        await self._conn.commit()

    async def cost_today(self, brain: str) -> float:
        """Actual USD charged for ``brain`` today (OpenRouter-reported cost, summed)."""
        cursor = await self._conn.execute(
            "SELECT cost_usd FROM usage WHERE date = ? AND brain = ?", (_today(), brain)
        )
        row = await cursor.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    # --- small key/value bot state (presence outfit, etc.) ---

    async def get_meta(self, key: str) -> str | None:
        """Read one persisted bot-state value (opaque string), or None if unset."""
        cursor = await self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        """Upsert one persisted bot-state value. Not a time-series table — never pruned."""
        await self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._conn.commit()

    # --- ambient own-thread memory (§8) ---

    async def recent_ambient(
        self, user_id: int, channel_id: int, limit: int = 12
    ) -> list[dict[str, Any]]:
        """The most recent ambient exchanges for this user+channel, oldest first."""
        cursor = await self._conn.execute(
            "SELECT role, content FROM ambient_log WHERE user_id = ? AND channel_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, channel_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in reversed(rows)]

    async def add_ambient(self, user_id: int, channel_id: int, role: str, content: str) -> None:
        await self._conn.execute(
            "INSERT INTO ambient_log (ts, user_id, channel_id, role, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), user_id, channel_id, role, content),
        )
        await self._conn.commit()

    # --- admin conversation memory (owner multi-turn continuity) ---

    async def recent_admin(
        self, user_id: int, channel_id: int, limit: int = 8
    ) -> list[dict[str, Any]]:
        """The most recent admin request/answer turns for this owner+channel, oldest first."""
        cursor = await self._conn.execute(
            "SELECT role, content FROM admin_log WHERE user_id = ? AND channel_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, channel_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in reversed(rows)]

    async def add_admin(self, user_id: int, channel_id: int, role: str, content: str) -> None:
        await self._conn.execute(
            "INSERT INTO admin_log (ts, user_id, channel_id, role, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), user_id, channel_id, role, content),
        )
        await self._conn.commit()

    # --- gigabrain conversation memory (owner multi-turn continuity) ---

    async def recent_gigabrain(
        self, user_id: int, channel_id: int, limit: int = 8
    ) -> list[dict[str, Any]]:
        """The most recent gigabrain request/answer turns for this owner+channel, oldest first."""
        cursor = await self._conn.execute(
            "SELECT role, content FROM gigabrain_log WHERE user_id = ? AND channel_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, channel_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in reversed(rows)]

    async def add_gigabrain(self, user_id: int, channel_id: int, role: str, content: str) -> None:
        await self._conn.execute(
            "INSERT INTO gigabrain_log (ts, user_id, channel_id, role, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), user_id, channel_id, role, content),
        )
        await self._conn.commit()

    # --- Scout item dedupe (§9) ---

    async def filter_unseen(self, feed_url: str, entry_ids: list[str]) -> set[str]:
        if not entry_ids:
            return set()
        placeholders = ",".join("?" * len(entry_ids))
        query = (
            "SELECT entry_id FROM seen WHERE feed_url = ? "  # noqa: S608 - placeholders only, no user data
            f"AND entry_id IN ({placeholders})"
        )
        cursor = await self._conn.execute(query, (feed_url, *entry_ids))
        seen = {row[0] for row in await cursor.fetchall()}
        return {entry_id for entry_id in entry_ids if entry_id not in seen}

    async def mark_seen(self, pairs: list[tuple[str, str]]) -> None:
        now = time.time()
        await self._conn.executemany(
            "INSERT OR IGNORE INTO seen (feed_url, entry_id, ts) VALUES (?, ?, ?)",
            [(feed_url, entry_id, now) for feed_url, entry_id in pairs],
        )
        await self._conn.commit()

    async def curated_delivery(self, local_date: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM curated_delivery WHERE local_date = ? ORDER BY id DESC LIMIT 1",
            (local_date,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def curated_delivery_state(self, local_date: str) -> dict[str, Any]:
        cursor = await self._conn.execute(
            "SELECT COUNT(*) AS used, "
            "COALESCE(SUM(status = 'pending'), 0) AS pending_today "
            "FROM curated_delivery WHERE local_date = ?", (local_date,),
        )
        state = dict(await cursor.fetchone())
        cursor = await self._conn.execute(
            "SELECT MAX(ts) AS last_claim_at, "
            "COALESCE(SUM(status = 'pending'), 0) AS pending_deliveries FROM curated_delivery"
        )
        return state | dict(await cursor.fetchone())

    async def curated_event_keys(self, event_keys: list[str]) -> set[str]:
        if not event_keys:
            return set()
        placeholders = ",".join("?" * len(event_keys))
        cursor = await self._conn.execute(
            f"SELECT event_key FROM curated_delivery WHERE event_key IN ({placeholders})",  # noqa: S608
            event_keys,
        )
        return {row[0] for row in await cursor.fetchall()}

    async def claim_curated_check(self, now: float) -> int | None:
        cursor = await self._conn.execute(
            "INSERT INTO curated_check (id, generation, expires_at) VALUES (1, 1, ?) "
            "ON CONFLICT(id) DO UPDATE SET generation = generation + 1, "
            "expires_at = excluded.expires_at WHERE expires_at <= ? RETURNING generation",
            (now + CURATED_CHECK_LEASE_SECONDS, now),
        )
        row = await cursor.fetchone()
        await cursor.close()
        await self._conn.commit()
        return int(row[0]) if row else None

    async def release_curated_check(self, generation: int) -> None:
        await self._conn.execute(
            "UPDATE curated_check SET expires_at = 0 WHERE id = 1 AND generation = ?",
            (generation,),
        )
        await self._conn.commit()

    async def curated_check_state(self) -> dict[str, Any]:
        cursor = await self._conn.execute(
            "SELECT local_date, observations, expires_at FROM curated_check WHERE id = 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else {"local_date": "", "observations": 0, "expires_at": 0}

    async def curated_observations(self, fingerprints: list[str]) -> dict[str, dict[str, Any]]:
        if not fingerprints:
            return {}
        placeholders = ",".join("?" * len(fingerprints))
        cursor = await self._conn.execute(
            f"SELECT * FROM curated_observation WHERE fingerprint IN ({placeholders})",  # noqa: S608
            fingerprints,
        )
        return {row["fingerprint"]: dict(row) for row in await cursor.fetchall()}

    async def begin_curated_observation(
        self, fingerprints: list[str], generation: int, now: float,
        *, local_date: str, max_observations: int,
    ) -> bool:
        admitted = await self._conn.execute(
            "UPDATE curated_check SET observations = CASE WHEN local_date = ? "
            "THEN observations + 1 ELSE 1 END, local_date = ? "
            "WHERE generation = ? AND expires_at > ? "
            "AND (local_date != ? OR observations < ?) RETURNING observations",
            (local_date, local_date, generation, now, local_date, max_observations),
        )
        row = await admitted.fetchone()
        await admitted.close()
        if row is None:
            await self._conn.commit()
            return False
        cursor = await self._conn.executemany(
            "INSERT INTO curated_observation (fingerprint, status, failures, retry_at, ts) "
            "SELECT ?, 'processing', 1, ?, ? WHERE EXISTS "
            "(SELECT 1 FROM curated_check WHERE generation = ? AND expires_at > ?) "
            "ON CONFLICT(fingerprint) DO UPDATE SET status = 'processing', "
            "failures = failures + 1, retry_at = excluded.retry_at, ts = excluded.ts",
            [(key, now + CURATED_CHECK_LEASE_SECONDS, now, generation, now)
             for key in fingerprints],
        )
        await self._conn.commit()
        return cursor.rowcount == len(fingerprints)

    async def finish_curated_observation(
        self, outcomes: list[tuple[str, str]], generation: int, now: float,
        *, retry_at: float = 0,
    ) -> None:
        await self._conn.executemany(
            "UPDATE curated_observation SET status = ?, failures = MAX(0, failures - ?), "
            "retry_at = ?, ts = ? WHERE fingerprint = ? AND EXISTS "
            "(SELECT 1 FROM curated_check WHERE generation = ?)",
            [(status, int(status == 'available'), retry_at, now, key, generation)
             for key, status in outcomes],
        )
        await self._conn.commit()

    async def claim_curated(
        self, local_date: str, feed_url: str, entry_id: str, *, event_key: str,
        max_posts: int, spacing_seconds: int, generation: int, now: float,
    ) -> int | None:
        """Atomically reserve capacity, spacing and item/event identity before Discord.

        A crash or uncertain send leaves `pending`; automatic retry could post a
        duplicate, so an operator must reconcile that state by hand.
        """
        try:
            cursor = await self._conn.execute(
                "INSERT OR IGNORE INTO curated_delivery "
                "(local_date, feed_url, entry_id, event_key, status, ts) "
                "SELECT ?, ?, ?, ?, 'pending', ? WHERE "
                "(SELECT COUNT(*) FROM curated_delivery WHERE local_date = ?) < ? "
                "AND NOT EXISTS (SELECT 1 FROM curated_delivery WHERE ts > ?) "
                "AND NOT EXISTS (SELECT 1 FROM curated_delivery WHERE feed_url = ? "
                "AND entry_id = ?) AND NOT EXISTS "
                "(SELECT 1 FROM seen WHERE feed_url = ? AND entry_id = ?) "
                "AND EXISTS (SELECT 1 FROM curated_check "
                "WHERE generation = ? AND expires_at > ?)",
                (local_date, feed_url, entry_id, event_key, now, local_date, max_posts,
                 now - spacing_seconds, feed_url, entry_id, feed_url, entry_id, generation, now),
            )
            if cursor.rowcount != 1:
                await self._conn.commit()
                return None
            await self._conn.commit()
            return cursor.lastrowid
        except Exception:
            await self._conn.rollback()
            raise

    async def mark_curated_sent(self, delivery_id: int, message_id: int) -> None:
        cursor = await self._conn.execute(
            "UPDATE curated_delivery SET status = 'sent', message_id = ? "
            "WHERE id = ? AND status = 'pending'",
            (str(message_id), delivery_id),
        )
        if cursor.rowcount != 1:
            await self._conn.rollback()
            raise RuntimeError("curated delivery claim missing")
        await self._conn.commit()

    # --- retention (§ backlog 1.3) ---

    async def prune(self, *, now: float | None = None) -> dict[str, int]:
        """Delete rows past their retention window; reclaim space. Returns rows removed per table.

        Idempotent: a second run finds nothing left to delete. ``VACUUM`` runs outside any
        transaction (after the commit) so it can actually shrink the file on disk.
        """
        cutoff_now = time.time() if now is None else now
        deleted: dict[str, int] = {}
        for table, days in RETENTION_DAYS.items():
            cutoff = cutoff_now - days * _DAY_SECONDS
            # table names come from the fixed RETENTION_DAYS dict above, never user input.
            cursor = await self._conn.execute(
                f"DELETE FROM {table} WHERE ts < ?",  # noqa: S608
                (cutoff,),
            )
            deleted[table] = cursor.rowcount
            await cursor.close()  # VACUUM refuses to run with any statement still in progress
        await self._conn.commit()
        checkpoint = await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        await checkpoint.close()
        await self._conn.execute("VACUUM")
        return deleted
