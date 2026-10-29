"""Persistence layer (aiosqlite, WAL). One writable file under ``/data``.

The full schema (§10 of the spec) is created up front so later phases add behaviour, not
migrations. P1 uses the ``audit`` table; ``seen`` / ``usage`` / ``ambient_log`` come online with
the digest, budgets, and ambient memory respectively.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
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
    message_url TEXT,
    story_decision_id INTEGER,
    story_ids_json TEXT,
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

CREATE TABLE IF NOT EXISTS curated_story (
    story_id   TEXT PRIMARY KEY,
    data_json  TEXT NOT NULL,
    ts         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS curated_story_decision (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    action         TEXT NOT NULL,
    reason_json    TEXT NOT NULL,
    change_json    TEXT NOT NULL,
    story_ids_json TEXT NOT NULL,
    claims_json    TEXT NOT NULL,
    sources_json   TEXT NOT NULL,
    event_key      TEXT,
    correction_of  INTEGER,
    ts             REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    date       TEXT    NOT NULL,
    brain      TEXT    NOT NULL,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd   REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (date, brain)
);

CREATE TABLE IF NOT EXISTS curated_model_attempt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    admission_day TEXT NOT NULL,
    role TEXT NOT NULL,
    profile TEXT NOT NULL,
    reserved_input INTEGER NOT NULL,
    reserved_output INTEGER NOT NULL,
    reserved_cost_units INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'reserved',
    actual_input INTEGER,
    actual_output INTEGER,
    actual_cost_usd TEXT,
    actual_cost_units INTEGER,
    settled_day TEXT,
    created_at REAL NOT NULL,
    deadline REAL NOT NULL,
    settled_at REAL
);
CREATE INDEX IF NOT EXISTS curated_attempt_workflow ON curated_model_attempt(workflow_id);
CREATE INDEX IF NOT EXISTS curated_attempt_day ON curated_model_attempt(admission_day);

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

_USD_UNITS = 1_000_000_000


def _cost_units(value: Any, rounding: str = ROUND_CEILING) -> int:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("cost must be finite and nonnegative")
        units = int((amount * _USD_UNITS).to_integral_value(rounding=rounding))
        if units > 2**63 - 1:
            raise ValueError("cost exceeds storage limit")
        return units
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid cost") from exc


def _curated_attempt_params(
    *, workflow_id: str, role: str, profile: str, input_tokens: int, output_tokens: int,
    cost_usd: Any, token_cap: int, usd_cap: Any, max_attempts: int, max_workflows: int,
    timeout_seconds: float, now: float | None = None,
) -> dict[str, Any]:
    for name, value in (("workflow_id", workflow_id), ("profile", profile)):
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value):
            raise ValueError(f"invalid {name}")
    if role not in {"writer", "reviewer"}:
        raise ValueError("invalid curated role")
    for value in (input_tokens, output_tokens, token_cap, max_attempts, max_workflows):
        if type(value) is not int or not 0 <= value <= 2**31 - 1:
            raise ValueError("token and attempt limits must be nonnegative integers")
    if not 0 < input_tokens <= 32_768 or not 0 < output_tokens <= 16_384:
        raise ValueError("invalid curated token reservation")
    now = time.time() if now is None else now
    if not math.isfinite(now) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("invalid curated reservation deadline")
    return {
        "workflow_id": workflow_id, "role": role, "profile": profile,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cost_units": _cost_units(cost_usd), "token_cap": token_cap,
        "usd_cap_units": _cost_units(usd_cap, ROUND_FLOOR),
        "max_attempts": max_attempts, "max_workflows": max_workflows,
        "day": _today(), "now": now, "deadline": now + timeout_seconds,
    }


_CURATED_BUDGET_SQL = """
WITH ledger AS (
    SELECT *, MAX(reserved_input, COALESCE(actual_input, 0))
            + MAX(reserved_output, COALESCE(actual_output, 0)) AS exposure_tokens,
           MAX(reserved_cost_units, COALESCE(actual_cost_units, 0)) AS exposure_cost_units
    FROM curated_model_attempt
), budget AS (
    SELECT
      COALESCE((SELECT tokens_in + tokens_out FROM usage
                WHERE brain = 'curated' AND date = :day), 0)
        + COALESCE(SUM(CASE WHEN state = 'settled' AND settled_day = :day
                           THEN actual_input + actual_output ELSE 0 END), 0) AS used_tokens,
      curated_cost_units(COALESCE((SELECT cost_usd FROM usage
                                  WHERE brain = 'curated' AND date = :day), 0))
        + COALESCE(SUM(CASE WHEN state = 'settled' AND settled_day = :day
                           THEN actual_cost_units ELSE 0 END), 0) AS used_cost_units,
      COALESCE((SELECT cost_usd FROM usage WHERE brain = 'curated' AND date = :day), 0)
        + COALESCE(SUM(CASE WHEN state = 'settled' AND settled_day = :day
                           THEN CAST(actual_cost_usd AS REAL) ELSE 0 END), 0) AS used_usd,
      COALESCE(SUM(CASE WHEN state = 'reserved' AND deadline > :now
                       THEN exposure_tokens ELSE 0 END), 0) AS reserved_tokens,
      COALESCE(SUM(CASE WHEN state = 'reserved' AND deadline > :now
                       THEN exposure_cost_units ELSE 0 END), 0) AS reserved_cost_units,
      COALESCE(SUM(CASE WHEN state IN ('uncertain', 'overrun')
                            OR (state = 'reserved' AND deadline <= :now)
                       THEN exposure_tokens ELSE 0 END), 0) AS unknown_tokens,
      COALESCE(SUM(CASE WHEN state IN ('uncertain', 'overrun')
                            OR (state = 'reserved' AND deadline <= :now)
                       THEN exposure_cost_units ELSE 0 END), 0) AS unknown_cost_units,
      COUNT(CASE WHEN state IN ('uncertain', 'overrun')
                      OR (state = 'reserved' AND deadline <= :now) THEN 1 END) AS unknown_attempts,
      COUNT(CASE WHEN state = 'overrun' THEN 1 END) AS overrun_attempts,
      COUNT(CASE WHEN admission_day = :day THEN 1 END) AS attempts_today,
      COUNT(DISTINCT CASE WHEN admission_day = :day THEN workflow_id END) AS workflows_today,
      COUNT(CASE WHEN workflow_id = :workflow_id THEN 1 END) AS workflow_attempts,
      COUNT(CASE WHEN workflow_id = :workflow_id AND admission_day = :day THEN 1 END)
        AS workflow_attempts_today
    FROM ledger
)
"""

_CURATED_DENIAL_SQL = """
CASE
  WHEN overrun_attempts > 0 THEN 'reservation_overrun'
  WHEN :token_cap <= 0 OR :usd_cap_units <= 0 OR :max_attempts <= 0 OR :max_workflows <= 0
    THEN 'disabled'
  WHEN workflow_attempts >= 4 THEN 'workflow_attempts'
  WHEN attempts_today >= :max_attempts THEN 'daily_attempts'
  WHEN workflow_attempts_today = 0 AND workflows_today >= :max_workflows THEN 'daily_workflows'
  WHEN used_tokens + reserved_tokens + unknown_tokens + :input_tokens + :output_tokens > :token_cap
    THEN 'tokens'
  WHEN used_cost_units + reserved_cost_units + unknown_cost_units + :cost_units > :usd_cap_units
    THEN 'usd'
  ELSE NULL
END
"""

_CLAIM_CURATED_SQL = """
WITH selected AS (
    SELECT id, action, event_key, story_ids_json
    FROM curated_story_decision
    WHERE id = :decision_id
)
INSERT OR IGNORE INTO curated_delivery
    (local_date, feed_url, entry_id, event_key, status, story_decision_id,
     story_ids_json, ts)
SELECT :local_date, :feed_url, :entry_id, :event_key, 'pending', :decision_id,
       (SELECT story_ids_json FROM selected), :now
WHERE (SELECT COUNT(*) FROM curated_delivery WHERE local_date = :local_date) < :max_posts
  AND NOT EXISTS (SELECT 1 FROM curated_delivery WHERE ts > :spacing_cutoff)
  AND NOT EXISTS (
      SELECT 1 FROM curated_delivery
      WHERE status = 'pending' AND feed_url = :feed_url AND entry_id = :entry_id
  )
  AND (
      (:decision_id IS NULL
       AND NOT EXISTS (
           SELECT 1 FROM curated_delivery WHERE feed_url = :feed_url AND entry_id = :entry_id
       )
       AND NOT EXISTS (
           SELECT 1 FROM seen WHERE feed_url = :feed_url AND entry_id = :entry_id
       ))
      OR
      (:decision_id IS NOT NULL AND EXISTS (
          SELECT 1 FROM selected sd
          WHERE sd.action IN ('publish', 'update', 'combine')
            AND sd.event_key = :event_key
            AND (sd.action != 'publish' OR NOT EXISTS (
                SELECT 1 FROM curated_delivery covered
                WHERE covered.status = 'sent' AND covered.message_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1
                      FROM json_each(sd.story_ids_json) current_story
                      JOIN json_each(covered.story_ids_json) covered_story
                        ON covered_story.value = current_story.value
                  )
            ))
            AND NOT EXISTS (
                SELECT 1 FROM curated_delivery pending
                WHERE pending.status = 'pending'
                  AND EXISTS (
                      SELECT 1
                      FROM json_each(sd.story_ids_json) current_story
                      JOIN json_each(pending.story_ids_json) pending_story
                        ON pending_story.value = current_story.value
                  )
            )
            AND (
                (sd.action IN ('publish', 'combine')
                 AND NOT EXISTS (
                     SELECT 1 FROM curated_delivery
                     WHERE feed_url = :feed_url AND entry_id = :entry_id
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM seen WHERE feed_url = :feed_url AND entry_id = :entry_id
                 ))
                OR
                (sd.action IN ('update', 'combine') AND EXISTS (
                    SELECT 1 FROM curated_delivery covered
                    WHERE covered.status = 'sent' AND covered.message_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1
                          FROM json_each(sd.story_ids_json) current_story
                          JOIN json_each(covered.story_ids_json) covered_story
                            ON covered_story.value = current_story.value
                      )
                ))
            )
      ))
  )
  AND EXISTS (
      SELECT 1 FROM curated_check WHERE generation = :generation AND expires_at > :now
  )
RETURNING id
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
    "gigabrain_log": 30,
    "seen": 90,
    "audit": 365,
    "curated_observation": 7,
    "curated_story": 90,
    "curated_story_decision": 90,
}

CURATED_STORY_LIMIT = 100
CURATED_STORY_DECISION_LIMIT = 500


class Store:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None
        self._curated_db: aiosqlite.Connection | None = None

    async def open(self) -> Store:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        memory = self._path == ":memory:"
        path = f"file:roger-{uuid.uuid4().hex}?mode=memory&cache=shared" if memory else self._path
        self._db = await aiosqlite.connect(path, uri=memory)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(_SCHEMA + _CURATED_DELIVERY_SCHEMA + ";")
        await self._migrate()
        await self._db.commit()
        # Legacy methods can roll back the shared connection between awaits. The
        # admission ledger must commit independently before any provider request starts.
        self._curated_db = await aiosqlite.connect(path, uri=memory, isolation_level=None)
        self._curated_db.row_factory = aiosqlite.Row
        await self._curated_db.create_function(
            "curated_cost_units", 1, _cost_units, deterministic=True,
        )
        return self

    async def close(self) -> None:
        if self._curated_db is not None:
            await self._curated_db.close()
            self._curated_db = None
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store used before open()")
        return self._db

    @property
    def _curated_conn(self) -> aiosqlite.Connection:
        if self._curated_db is None:
            raise RuntimeError("Store used before open()")
        return self._curated_db

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
        if not await self._has_column("curated_delivery", "message_url"):
            await self._conn.execute("ALTER TABLE curated_delivery ADD COLUMN message_url TEXT")
        if not await self._has_column("curated_delivery", "story_decision_id"):
            await self._conn.execute(
                "ALTER TABLE curated_delivery ADD COLUMN story_decision_id INTEGER"
            )
        if not await self._has_column("curated_delivery", "story_ids_json"):
            await self._conn.execute(
                "ALTER TABLE curated_delivery ADD COLUMN story_ids_json TEXT"
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
        if brain == "curated":
            return int((await self.curated_budget_state())["used_tokens"])
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
        if brain == "curated":
            return float((await self.curated_budget_state())["used_usd"])
        cursor = await self._conn.execute(
            "SELECT cost_usd FROM usage WHERE date = ? AND brain = ?", (_today(), brain)
        )
        row = await cursor.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    async def reserve_curated_attempt(self, **request: Any) -> int | None:
        """Commit a bound before provider I/O. All unresolved days count against today's caps.

        ``_curated_attempt_params`` defines the admission fields. Admission and settlement
        use the existing host-local budget day, independently of the posting timezone.
        The separate connection commits the conditional INSERT before returning its ID.
        """
        params = _curated_attempt_params(**request)
        rows = await self._curated_conn.execute_fetchall(
            _CURATED_BUDGET_SQL  # noqa: S608 - only code-owned SQL fragments
            + """
            INSERT INTO curated_model_attempt
                (workflow_id, admission_day, role, profile, reserved_input, reserved_output,
                 reserved_cost_units, created_at, deadline)
            SELECT :workflow_id, :day, :role, :profile, :input_tokens, :output_tokens,
                   :cost_units, :now, :deadline FROM budget
            WHERE (""" + _CURATED_DENIAL_SQL + ") IS NULL RETURNING id", params,
        )
        return int(rows[0][0]) if rows else None

    async def finish_curated_attempt(
        self, attempt_id: int, *, input_tokens: Any = None, output_tokens: Any = None,
        cost_usd: Any = None, uncertain: bool = False, now: float | None = None,
    ) -> str | None:
        """Settle once, or keep uncertain exposure. A known overrun retains the larger charge.

        Callers supply only trustworthy provider usage, even when the content was rejected.
        Partial valid usage is retained without releasing any unaccounted reservation.
        Like the legacy usage totals, this payload-free ledger is not automatically pruned.
        """
        actual_input = (input_tokens if type(input_tokens) is int
                        and 0 <= input_tokens <= 2**63 - 1 else None)
        actual_output = (output_tokens if type(output_tokens) is int
                         and 0 <= output_tokens <= 2**63 - 1 else None)
        try:
            actual_cost_units = _cost_units(cost_usd)
            actual_cost = str(Decimal(str(cost_usd)))
        except ValueError:
            actual_cost_units, actual_cost = None, None
        complete = all(value is not None for value in (
            actual_input, actual_output, actual_cost_units,
        ))
        rows = await self._curated_conn.execute_fetchall(
            """UPDATE curated_model_attempt SET
                 state = CASE WHEN :input > reserved_input OR :output > reserved_output
                                   OR :cost_units > reserved_cost_units THEN 'overrun'
                              WHEN NOT :complete OR :uncertain THEN 'uncertain'
                              ELSE 'settled' END,
                 actual_input = :input, actual_output = :output,
                 actual_cost_usd = :cost, actual_cost_units = :cost_units,
                 settled_day = :day, settled_at = :now
               WHERE id = :id AND state = 'reserved' RETURNING state""",
            {"id": attempt_id, "complete": complete, "uncertain": uncertain, "input": actual_input,
             "output": actual_output, "cost": actual_cost, "cost_units": actual_cost_units,
             "day": _today(), "now": time.time() if now is None else now},
        )
        return str(rows[0][0]) if rows else None

    async def mark_curated_attempt_uncertain(self, attempt_id: int) -> str | None:
        return await self.finish_curated_attempt(attempt_id)

    async def curated_budget_state(
        self, *, now: float | None = None, request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Used, reserved, and unknown exposure; optionally diagnose a refused request.

        A diagnosis reflects this snapshot. Concurrent settlement may clear the gate after
        a refusal. Deadline expiry only changes the displayed category, never the liability.
        """
        params = {"day": _today(), "now": time.time() if now is None else now, "workflow_id": ""}
        if request is not None:
            params = _curated_attempt_params(**request)
            if now is not None:
                params["now"] = now
        rows = await self._curated_conn.execute_fetchall(
            _CURATED_BUDGET_SQL + "SELECT *, "  # noqa: S608 - only code-owned SQL fragments
            + (_CURATED_DENIAL_SQL if request is not None else "NULL")
            + " AS denial_reason FROM budget", params,
        )
        state = dict(rows[0])
        state["reserved_usd"] = state["reserved_cost_units"] / _USD_UNITS
        state["unknown_usd"] = state["unknown_cost_units"] / _USD_UNITS
        return state

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
        story_decision_id: int | None = None,
    ) -> int | None:
        """Atomically reserve capacity, spacing and item/event identity before Discord.

        A crash or uncertain send leaves `pending`; automatic retry could post a
        duplicate, so an operator must reconcile that state by hand.
        """
        try:
            cursor = await self._conn.execute(
                _CLAIM_CURATED_SQL,
                {
                    "local_date": local_date, "feed_url": feed_url, "entry_id": entry_id,
                    "event_key": event_key, "decision_id": story_decision_id, "now": now,
                    "max_posts": max_posts, "spacing_cutoff": now - spacing_seconds,
                    "generation": generation,
                },
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                await self._conn.commit()
                return None
            await self._conn.commit()
            return int(row[0])
        except Exception:
            await self._conn.rollback()
            raise

    async def mark_curated_sent(
        self, delivery_id: int, message_id: int, *, message_url: str | None = None,
    ) -> None:
        cursor = await self._conn.execute(
            "UPDATE curated_delivery SET status = 'sent', message_id = ?, message_url = ? "
            "WHERE id = ? AND status = 'pending'",
            (str(message_id), message_url, delivery_id),
        )
        if cursor.rowcount != 1:
            await self._conn.rollback()
            raise RuntimeError("curated delivery claim missing")
        await self._conn.commit()

    async def curated_story_context(
        self, story_ids: list[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        ids = list(dict.fromkeys(story_ids or []))
        if ids:
            encoded_ids = json.dumps(ids)
            cursor = await self._conn.execute(
                "SELECT story_id, data_json, ts FROM curated_story WHERE "
                "story_id IN (SELECT value FROM json_each(?)) OR EXISTS ("
                "SELECT 1 FROM json_each(data_json, '$.sources') source "
                "WHERE json_extract(source.value, '$.story_id') IN "
                "(SELECT value FROM json_each(?))) ORDER BY ts DESC LIMIT ?",
                (encoded_ids, encoded_ids, CURATED_STORY_LIMIT),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT story_id, data_json, ts FROM curated_story ORDER BY ts DESC LIMIT ?",
                (CURATED_STORY_LIMIT,),
            )
        stories = [
            {"story_id": row["story_id"], "data": json.loads(row["data_json"]), "ts": row["ts"]}
            for row in await cursor.fetchall()
        ]
        resolved_ids = list(dict.fromkeys([*ids, *(story["story_id"] for story in stories)]))
        base = (
            "SELECT sd.*, d.message_id, d.message_url FROM curated_story_decision sd "
            "JOIN curated_delivery d ON d.story_decision_id = sd.id "
            "WHERE d.status = 'sent' AND d.message_id IS NOT NULL "
        )
        if ids:
            encoded_resolved_ids = json.dumps(resolved_ids)
            cursor = await self._conn.execute(
                "SELECT sd.*, d.message_id, d.message_url FROM curated_story_decision sd "
                "JOIN curated_delivery d ON d.story_decision_id = sd.id "
                "WHERE d.status = 'sent' AND d.message_id IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM json_each(sd.story_ids_json) "
                "WHERE value IN (SELECT value FROM json_each(?))) "
                "ORDER BY d.ts DESC LIMIT 4",
                (encoded_resolved_ids,),
            )
        else:
            cursor = await self._conn.execute(base + "ORDER BY d.ts DESC LIMIT 4")
        delivered = []
        for row in await cursor.fetchall():
            item = dict(row)
            for key in (
                "reason_json", "change_json", "story_ids_json", "claims_json", "sources_json",
            ):
                item[key.removesuffix("_json")] = json.loads(item.pop(key))
            delivered.append(item)
        return {"stories": stories, "delivered": delivered}

    async def record_curated_story_decision(
        self, decision: dict[str, Any], sources: list[dict[str, Any]], *, now: float,
    ) -> int:
        """Append an editorial decision and refresh its bounded current story pointers."""
        cursor = await self._conn.execute(
            "INSERT INTO curated_story_decision "
            "(action, reason_json, change_json, story_ids_json, claims_json, sources_json, "
            "event_key, correction_of, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (decision["action"], json.dumps(decision["reason"]), json.dumps(decision["change"]),
             json.dumps(decision["story_ids"]), json.dumps(decision.get("claims", [])),
             json.dumps(sources), decision.get("event_key"), decision.get("correction_of"), now),
        )
        decision_id = int(cursor.lastrowid)
        citations = [*decision["reason"]["citations"], *decision["change"]["citations"]]
        for claim in decision.get("claims", []):
            citations.extend(claim["citations"])
        cited = {citation["source"] - 1 for citation in citations}
        pointer_sources = [
            source for index, source in enumerate(sources)
            if index in cited or source.get("story_id") in decision["story_ids"]
        ][:8]
        data = json.dumps({
            "sources": pointer_sources,
            "unresolved_questions": decision.get("unresolved_questions", []),
            "last_decision_id": decision_id,
        })
        await self._conn.executemany(
            "INSERT INTO curated_story (story_id, data_json, ts) VALUES (?, ?, ?) "
            "ON CONFLICT(story_id) DO UPDATE SET data_json = excluded.data_json, ts = excluded.ts",
            [(story_id, data, now) for story_id in decision["story_ids"]],
        )
        await self._conn.execute(
            "DELETE FROM curated_story WHERE story_id IN (SELECT story_id FROM curated_story "
            "ORDER BY ts DESC LIMIT -1 OFFSET ?)",
            (CURATED_STORY_LIMIT,),
        )
        await self._conn.execute(
            "DELETE FROM curated_story_decision WHERE id IN "
            "(SELECT id FROM curated_story_decision ORDER BY ts DESC, id DESC LIMIT -1 OFFSET ?)",
            (CURATED_STORY_DECISION_LIMIT,),
        )
        await self._conn.commit()
        return decision_id

    async def curated_story_decisions(self) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM curated_story_decision ORDER BY id"
        )
        return [dict(row) for row in await cursor.fetchall()]

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
