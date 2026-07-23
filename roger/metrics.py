"""Prometheus metrics and an in-process ``/metrics`` endpoint (backlog 3.1).

Two kinds of series, modelled the way Prometheus wants them:

- **Event counters** (LLM requests / errors / budget rejections) are incremented at the call site,
  resetting on restart — ordinary monotonic counters; `rate()` handles the resets.
- **State gauges** (today's token/dollar spend, caps, feed count, audit tallies, build info) are
  sourced from SQLite and refreshed on a timer, so they survive restarts and mirror the store.

``prometheus_client`` serves both from a background WSGI thread; nothing here runs on the asyncio
loop except the async :func:`refresh`. The store stays the source of truth — this only exposes it.
"""

from __future__ import annotations

import logging
from typing import Any
from wsgiref.simple_server import WSGIServer

from prometheus_client import Counter, Gauge, start_http_server

log = logging.getLogger("roger.metrics")

_BRAINS = ("admin", "ambient", "digest")

# --- event counters (in-process; incremented at the call site) ---
LLM_REQUESTS = Counter("roger_llm_requests_total", "LLM completion calls dispatched", ["brain"])
LLM_ERRORS = Counter(
    "roger_llm_errors_total", "LLM calls that failed after retries", ["brain", "type"]
)
LLM_BUDGET_EXCEEDED = Counter(
    "roger_llm_budget_exceeded_total", "Calls refused by the daily token budget", ["brain"]
)

# --- state gauges (refreshed from SQLite on a timer) ---
TOKENS_TODAY = Gauge("roger_tokens_today", "Tokens spent today", ["brain"])
TOKENS_CAP = Gauge("roger_tokens_cap", "Daily token cap", ["brain"])
COST_USD_TODAY = Gauge("roger_cost_usd_today", "USD spent today (OpenRouter-reported)", ["brain"])
FEEDS = Gauge("roger_feeds", "Curated digest feeds")
AUDIT_EVENTS = Gauge("roger_audit_events", "Audit rows in the retention window", ["tool", "status"])
BUILD_INFO = Gauge("roger_build_info", "Deployed build; value is always 1", ["version"])


async def refresh(store: Any, settings: Any, version: str) -> None:
    """Repopulate the SQLite-sourced gauges. Cheap; called once at startup and then on a timer."""
    caps = {
        "admin": settings.daily_tokens_admin,
        "ambient": settings.daily_tokens_ambient,
        "digest": settings.daily_tokens_digest,
    }
    for brain in _BRAINS:
        TOKENS_TODAY.labels(brain).set(await store.usage_today(brain))
        COST_USD_TODAY.labels(brain).set(await store.cost_today(brain))
        TOKENS_CAP.labels(brain).set(caps[brain])
    FEEDS.set(await store.count_feeds())
    # Rebuild the audit series from scratch so a (tool, status) combo that drops to zero after a
    # prune doesn't linger as a stale sample.
    AUDIT_EVENTS.clear()
    for row in await store.audit_tally():
        AUDIT_EVENTS.labels(row["tool"] or "none", row["status"]).set(row["count"])
    BUILD_INFO.clear()
    BUILD_INFO.labels(version).set(1)


def start_server(port: int) -> WSGIServer:
    """Start the metrics HTTP server on a background daemon thread; return it for shutdown."""
    server, _thread = start_http_server(port)
    log.info("metrics server listening on :%d/metrics", port)
    return server
