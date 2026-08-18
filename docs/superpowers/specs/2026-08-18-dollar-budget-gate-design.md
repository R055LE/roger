# Dollar-denominated budget gate — design

Tracks BACKLOG.md 1.1. Visibility (`cost_usd` tracked per brain/day, surfaced in `/status`) already
shipped; this closes the gap — the daily gate itself still only enforces on raw tokens, which is a
weak proxy once a brain's model chain mixes models at very different prices.

## Goal

Let each brain optionally cap on real USD spend instead of (or alongside) token count, without
losing the safety the token cap already provides when a provider doesn't report cost.

## Semantics: layered, not replacing

Token cap keeps enforcing unconditionally, exactly as today. If `DAILY_USD_<BRAIN>` is also set for
a brain, a second, independent check runs against `cost_today(brain)`. Whichever trips first wins.

This was chosen over "$ cap replaces token cap when set" because a provider that never reports cost
(e.g. `OPENROUTER_BASE_URL` pointed at a local host, per ADR-0009) would otherwise leave that brain
with no effective cap at all. Layering means the $ check simply never trips in that case, and the
token cap remains the real backstop — no code needs to detect "is cost data flowing."

## Config

`roger/config.py`, four new settings:

```
daily_usd_admin: float = 0.0
daily_usd_ambient: float = 0.0
daily_usd_digest: float = 0.0
daily_usd_gigabrain: float = 0.0
```

`0.0` = disabled/opt-in, matching the existing `gigabrain_interval_days = 0` convention. Defaults
stay 0 rather than shipping real dollar figures — no behavior change on the live host until the
owner sets a number that matches their actual OpenRouter spend.

`roger.env.example` gets a `DAILY_USD_*` block next to the existing `DAILY_TOKENS_*` block, commented
to explain the opt-in default.

## `BudgetExceeded`

Gains a keyword-only `unit: str = "tokens"` field (`"tokens"` or `"usd"`), so callers can report
which cap actually tripped instead of assuming it was always tokens. Existing 2-positional-arg call
sites (tests, all four brain modules) stay valid unchanged since `unit` defaults.

## Gate (`LLM.complete`)

```
used = await self._store.usage_today(brain)
cap = self._caps[brain]
if used >= cap:
    metrics.LLM_BUDGET_EXCEEDED.labels(brain, "tokens").inc()
    raise BudgetExceeded(brain, used, cap, unit="tokens")

usd_cap = self._usd_caps[brain]
if usd_cap > 0:
    spent = await self._store.cost_today(brain)
    if spent >= usd_cap:
        metrics.LLM_BUDGET_EXCEEDED.labels(brain, "usd").inc()
        raise BudgetExceeded(brain, spent, usd_cap, unit="usd")
```

The extra `cost_today` read only happens when a $ cap is configured for that brain — one cheap local
SQLite read, not on the hot path otherwise.

## Message / audit text

Three spots currently hardcode "token" and would mislead once a $ cap can be the trigger:

- `roger/brains/admin.py` audit `detail="daily token cap"` → `f"daily {exc.unit} cap"`
- `roger/brains/gigabrain.py` audit detail: same fix; reply text drops "token", becomes generic
  ("I've hit my daily budget for gigabrain work...")
- `roger/brains/digest.py` log line: `"digest skipped: daily token budget hit"` → includes
  `exc.unit`

`roger/brains/ambient.py`'s `BUDGET_LINE` ("I'm out of words for today.") is already unit-agnostic —
no change.

## Ops alert (`bot.py:_budget_alert`, backlog 1.2)

Currently only watches token fraction; extending now so it doesn't go blind once a $ cap is set —
otherwise the alerting infra that already exists for exactly this would silently miss it.

Gains a keyword-only `usd_cap: float = 0.0` param. Computes both fractions
(`used/cap`, `cost/usd_cap`), alerts on `max()` of the two once it crosses `BUDGET_ALERT_FRACTION`
(0.8), message body shows both figures when both caps are configured.

Verified against the 4 existing `test_ops.py` cases — all pass unchanged with the new param
defaulting to 0 (disabled), since `max(token_frac, 0.0) == token_frac`.

The watchdog loop (`_watchdog`) passes a new `_daily_usd_caps(settings)` helper through, mirroring
`_daily_caps`.

## `/status`

Per-brain cost line grows a `/ $cap` suffix when a $ cap is configured for that brain:

```
$0.0842 / $2.0000
```

vs. today's bare `$0.0842` when no cap is set. `_format_status` and `gather_status` take a new
`usd_caps` dict, defaulting to `{}` so existing call sites in tests keep working.

## Metrics

- New gauge `roger_cost_usd_cap` (mirrors `roger_tokens_cap`), refreshed the same way.
- `roger_llm_budget_exceeded_total` gains a `reason` label (`tokens`/`usd`) so Grafana can show which
  cap is actually biting, not just that budget rejections are happening.

## Testing

Extends existing suites, no new test files:

- `test_llm.py` — $ cap trips when configured and cost is at/over it; token cap still works
  unconditionally when $ cap is unset; $ cap never trips when cost stays 0 (non-reporting provider).
- `test_ops.py` — new `_budget_alert` cases with `usd_cap` set (warn, exhausted, both-configured).
- `test_status.py` — usd-cap-column case.
- `test_config.py` — new settings default to 0.0.
- `test_metrics.py` — new gauge present; `reason` label on the counter.

## Out of scope

- Making the $ cap the *only* mechanism (see Semantics above — deliberately layered, not a
  replacement).
- Backlog 1.7 (on-demand tool-call budget override) — unrelated axis (blast-radius bound, not spend).
- Any change to how OpenRouter reports `cost` on the usage object — already handled, unchanged.
