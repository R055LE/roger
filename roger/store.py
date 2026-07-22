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
"""


def _today() -> str:
    return time.strftime("%Y-%m-%d")


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

    async def add_usage(self, brain: str, tokens_in: int, tokens_out: int) -> None:
        await self._conn.execute(
            "INSERT INTO usage (date, brain, tokens_in, tokens_out) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(date, brain) DO UPDATE SET "
            "tokens_in = tokens_in + excluded.tokens_in, "
            "tokens_out = tokens_out + excluded.tokens_out",
            (_today(), brain, tokens_in, tokens_out),
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
