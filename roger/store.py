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


class AuditStatus(StrEnum):
    OK = "ok"
    DENIED = "denied"
    INVALID = "invalid"
    ERROR = "error"
    GATE_REJECTED = "gate_rejected"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY,
    ts        REAL    NOT NULL,
    actor_id  INTEGER,
    brain     TEXT,
    tool      TEXT,
    args_json TEXT,
    status    TEXT    NOT NULL,
    detail    TEXT
);

CREATE TABLE IF NOT EXISTS seen (
    feed_url TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    ts       REAL NOT NULL,
    PRIMARY KEY (feed_url, entry_id)
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

CREATE TABLE IF NOT EXISTS feeds (
    url      TEXT PRIMARY KEY,
    title    TEXT,
    added_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _today() -> str:
    return time.strftime("%Y-%m-%d")


_DAY_SECONDS = 86_400

# Retention windows (days) for the time-series tables. `audit` is the tamper-evident trail, so it's
# kept the longest; ambient/admin conversation memory is short-lived by design (privacy + it stops
# being useful context quickly); `seen` only needs to outlive a feed's practical re-post window.
RETENTION_DAYS: dict[str, int] = {
    "ambient_log": 30,
    "admin_log": 30,
    "seen": 90,
    "audit": 365,
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
        await self._db.executescript(_SCHEMA)
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

        A DB provisioned before ``cost_usd`` existed already has the ``usage`` table, so the
        schema's ``CREATE TABLE IF NOT EXISTS`` skips it and the column must be added by hand. The
        column check makes a run against a fresh (already-current) DB a no-op.
        """
        if not await self._has_column("usage", "cost_usd"):
            await self._conn.execute(
                "ALTER TABLE usage ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0"
            )

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
            "INSERT INTO audit (ts, actor_id, brain, tool, args_json, status, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                actor_id,
                brain,
                tool,
                json.dumps(args, default=str) if args is not None else None,
                str(status),
                detail,
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

    # --- digest feed list (Roger-curated; seeded once from DIGEST_FEEDS) ---

    async def list_feeds(self) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT url, title, added_ts FROM feeds ORDER BY added_ts, url"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def count_feeds(self) -> int:
        cursor = await self._conn.execute("SELECT COUNT(*) FROM feeds")
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def add_feed(self, url: str, title: str | None) -> bool:
        """Insert a feed. Returns True if newly added, False if the URL already existed."""
        cursor = await self._conn.execute(
            "INSERT OR IGNORE INTO feeds (url, title, added_ts) VALUES (?, ?, ?)",
            (url, title, time.time()),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def remove_feed(self, url: str) -> bool:
        """Delete a feed by exact URL. Returns True if a row was removed."""
        cursor = await self._conn.execute("DELETE FROM feeds WHERE url = ?", (url,))
        await self._conn.commit()
        return cursor.rowcount > 0

    async def seed_feeds(self, urls: list[str]) -> int:
        now = time.time()
        await self._conn.executemany(
            "INSERT OR IGNORE INTO feeds (url, title, added_ts) VALUES (?, ?, ?)",
            [(url, None, now) for url in urls],
        )
        await self._conn.commit()
        return len(urls)

    # --- digest dedupe (§9) ---

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
