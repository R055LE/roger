# ADR-0003: Metrics as SQLite-sourced gauges + in-process counters

- **Status:** Accepted
- **Date:** 2026-07-23

## Context

Exposing Prometheus `/metrics` on an async bot, over three kinds of data: persistent daily state
(token/dollar spend, caps) that should survive restarts; event-rate data (LLM requests, errors,
budget rejections); and tool-call outcomes that already live in the `audit` table. `prometheus_client`
collectors are synchronous, but the store is async (`aiosqlite`).

## Decision

- **Persistent state → gauges**, refreshed from SQLite on a 30s async loop (survive restarts, mirror
  the store).
- **Event rates → in-process counters**, incremented at the call site (reset on restart; `rate()`
  handles resets).
- **Tool outcomes → sourced free from the `audit` table** on each refresh, no new instrumentation.
- Serve from `prometheus_client`'s background thread; only the async `refresh` touches the event loop.

## Consequences

- Spend numbers are restart-safe; rates get standard counter semantics; tool metrics cost nothing extra.
- Gauges are up to 30s stale — fine for these; a bot scrape doesn't need sub-second freshness.
- Avoided an async custom collector (would fight `collect()` being sync) by refreshing on a timer.
