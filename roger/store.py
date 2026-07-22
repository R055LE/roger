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
