# Dollar-Denominated Budget Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each brain optionally cap on real USD spend (`DAILY_USD_<BRAIN>`), layered on top of
the existing daily token cap rather than replacing it.

**Architecture:** Four new `Settings` fields feed a second, independent check in
`LLM.complete()` — the token cap keeps enforcing unconditionally exactly as today; if a brain also
has a `$` cap configured (`> 0`), a second read of `store.cost_today(brain)` gates the call too.
Whichever trips first wins. The same layered read feeds the existing ops-alert threshold and the
`/status` readout, so all three budget-aware surfaces (gate, alert, status) stay consistent.

**Tech Stack:** Python 3.14, pydantic-settings, pytest/pytest-asyncio, prometheus_client. No new
dependencies.

## Global Constraints

- `DAILY_USD_<BRAIN>` defaults to `0.0` (disabled/opt-in) — matches the `gigabrain_interval_days = 0`
  convention already in `roger/config.py`. No behavior change on deploy until explicitly set.
- Token cap always enforces, unconditionally, regardless of whether a `$` cap is set. This is the
  approved design (see `docs/superpowers/specs/2026-08-18-dollar-budget-gate-design.md`) — never
  make the `$` cap replace the token cap.
- Match existing code style exactly: docstrings in the file's existing voice, `f"..."` formatting
  conventions already used nearby, no new abstractions.
- Every task must leave `pytest` and `ruff check .` green before its commit.

---

### Task 1: `DAILY_USD_<BRAIN>` config

**Files:**
- Modify: `roger/config.py:39-43`
- Modify: `roger.env.example:27-31`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.daily_usd_admin`, `Settings.daily_usd_ambient`, `Settings.daily_usd_digest`,
  `Settings.daily_usd_gigabrain` — all `float`, default `0.0`. Consumed by Tasks 2, 3, 5, 6.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_daily_usd_defaults_to_disabled(monkeypatch):
    _set_required(monkeypatch)
    settings = Settings()
    assert settings.daily_usd_admin == 0.0
    assert settings.daily_usd_ambient == 0.0
    assert settings.daily_usd_digest == 0.0
    assert settings.daily_usd_gigabrain == 0.0


def test_daily_usd_parses_from_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("DAILY_USD_ADMIN", "2.5")
    assert Settings().daily_usd_admin == 2.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -v -k daily_usd`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'daily_usd_admin'`

- [ ] **Step 3: Add the settings fields**

In `roger/config.py`, find:

```python
    # --- budgets (daily in+out tokens per brain) ---
    daily_tokens_admin: int = 150_000
    daily_tokens_ambient: int = 40_000
    daily_tokens_digest: int = 30_000
    daily_tokens_gigabrain: int = 100_000
```

Replace with:

```python
    # --- budgets (daily in+out tokens per brain) ---
    daily_tokens_admin: int = 150_000
    daily_tokens_ambient: int = 40_000
    daily_tokens_digest: int = 30_000
    daily_tokens_gigabrain: int = 100_000

    # --- budgets (daily USD, layered on top of the token caps above) ---
    # 0 = disabled (opt-in); set to a real figure once OpenRouter cost data looks right for your
    # model mix. The token cap above keeps enforcing regardless — this is an additional, tighter
    # trip wire, not a replacement (a provider that never reports cost would otherwise leave the
    # brain with no effective cap at all).
    daily_usd_admin: float = 0.0
    daily_usd_ambient: float = 0.0
    daily_usd_digest: float = 0.0
    daily_usd_gigabrain: float = 0.0
```

- [ ] **Step 4: Add the env template block**

In `roger.env.example`, find:

```
# --- budgets (daily in+out tokens per brain) ---
DAILY_TOKENS_ADMIN=150000
DAILY_TOKENS_AMBIENT=40000
DAILY_TOKENS_DIGEST=30000
DAILY_TOKENS_GIGABRAIN=100000
```

Replace with:

```
# --- budgets (daily in+out tokens per brain) ---
DAILY_TOKENS_ADMIN=150000
DAILY_TOKENS_AMBIENT=40000
DAILY_TOKENS_DIGEST=30000
DAILY_TOKENS_GIGABRAIN=100000

# --- budgets (daily USD, layered on top of the token caps above; 0 = disabled/opt-in) ---
# the token cap keeps enforcing regardless of these — set a real figure per brain once you know
# what your model mix actually costs.
DAILY_USD_ADMIN=0
DAILY_USD_AMBIENT=0
DAILY_USD_DIGEST=0
DAILY_USD_GIGABRAIN=0
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 6: Commit**

```bash
git add roger/config.py roger.env.example tests/test_config.py
git commit -m "feat: add DAILY_USD_* config for the dollar budget gate"
```

---

### Task 2: budget metrics — USD cap gauge + unit-aware counter

**Files:**
- Modify: `roger/metrics.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `Settings.daily_usd_*` (Task 1).
- Produces: `metrics.COST_USD_CAP` gauge (labels: `brain`); `metrics.LLM_BUDGET_EXCEEDED` counter now
  takes two labels, `.labels(brain, reason)` where `reason` is `"tokens"` or `"usd"` — Task 3's call
  sites depend on this exact label arity.

- [ ] **Step 1: Write the failing tests**

In `tests/test_metrics.py`, find:

```python
def _settings():
    return SimpleNamespace(
        daily_tokens_admin=150_000,
        daily_tokens_ambient=40_000,
        daily_tokens_digest=30_000,
        daily_tokens_gigabrain=100_000,
    )
```

Replace with:

```python
def _settings():
    return SimpleNamespace(
        daily_tokens_admin=150_000,
        daily_tokens_ambient=40_000,
        daily_tokens_digest=30_000,
        daily_tokens_gigabrain=100_000,
        daily_usd_admin=0.0,
        daily_usd_ambient=0.0,
        daily_usd_digest=0.0,
        daily_usd_gigabrain=0.0,
    )
```

Then append a new test at the end of the file:

```python
async def test_refresh_populates_usd_cap_gauge(tmp_path):
    store = await Store(str(tmp_path / "m.db")).open()
    try:
        settings = _settings()
        settings.daily_usd_admin = 2.5

        await metrics.refresh(store, settings, "sha-test")

        get = REGISTRY.get_sample_value
        assert get("roger_cost_usd_cap", {"brain": "admin"}) == 2.5
        assert get("roger_cost_usd_cap", {"brain": "ambient"}) == 0.0
    finally:
        await store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_metrics.py -v -k usd_cap`
Expected: FAIL — `AssertionError` (metric `roger_cost_usd_cap` doesn't exist yet, `get_sample_value`
returns `None`)

- [ ] **Step 3: Add the gauge and widen the counter's labels**

In `roger/metrics.py`, find:

```python
LLM_BUDGET_EXCEEDED = Counter(
    "roger_llm_budget_exceeded_total", "Calls refused by the daily token budget", ["brain"]
)

# --- state gauges (refreshed from SQLite on a timer) ---
TOKENS_TODAY = Gauge("roger_tokens_today", "Tokens spent today", ["brain"])
TOKENS_CAP = Gauge("roger_tokens_cap", "Daily token cap", ["brain"])
COST_USD_TODAY = Gauge("roger_cost_usd_today", "USD spent today (OpenRouter-reported)", ["brain"])
```

Replace with:

```python
LLM_BUDGET_EXCEEDED = Counter(
    "roger_llm_budget_exceeded_total",
    "Calls refused by the daily budget",
    ["brain", "reason"],  # reason: "tokens" or "usd"
)

# --- state gauges (refreshed from SQLite on a timer) ---
TOKENS_TODAY = Gauge("roger_tokens_today", "Tokens spent today", ["brain"])
TOKENS_CAP = Gauge("roger_tokens_cap", "Daily token cap", ["brain"])
COST_USD_TODAY = Gauge("roger_cost_usd_today", "USD spent today (OpenRouter-reported)", ["brain"])
COST_USD_CAP = Gauge("roger_cost_usd_cap", "Daily USD cap (0 = disabled)", ["brain"])
```

- [ ] **Step 4: Refresh the new gauge**

In `roger/metrics.py`, find:

```python
async def refresh(store: Any, settings: Any, version: str) -> None:
    """Repopulate the SQLite-sourced gauges. Cheap; called once at startup and then on a timer."""
    caps = {
        "admin": settings.daily_tokens_admin,
        "ambient": settings.daily_tokens_ambient,
        "digest": settings.daily_tokens_digest,
        "gigabrain": settings.daily_tokens_gigabrain,
    }
    for brain in _BRAINS:
        TOKENS_TODAY.labels(brain).set(await store.usage_today(brain))
        COST_USD_TODAY.labels(brain).set(await store.cost_today(brain))
        TOKENS_CAP.labels(brain).set(caps[brain])
```

Replace with:

```python
async def refresh(store: Any, settings: Any, version: str) -> None:
    """Repopulate the SQLite-sourced gauges. Cheap; called once at startup and then on a timer."""
    caps = {
        "admin": settings.daily_tokens_admin,
        "ambient": settings.daily_tokens_ambient,
        "digest": settings.daily_tokens_digest,
        "gigabrain": settings.daily_tokens_gigabrain,
    }
    usd_caps = {
        "admin": settings.daily_usd_admin,
        "ambient": settings.daily_usd_ambient,
        "digest": settings.daily_usd_digest,
        "gigabrain": settings.daily_usd_gigabrain,
    }
    for brain in _BRAINS:
        TOKENS_TODAY.labels(brain).set(await store.usage_today(brain))
        COST_USD_TODAY.labels(brain).set(await store.cost_today(brain))
        TOKENS_CAP.labels(brain).set(caps[brain])
        COST_USD_CAP.labels(brain).set(usd_caps[brain])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_metrics.py -v`
Expected: PASS (all tests in the file — note `test_completion_increments_the_request_counter` and
the other pre-existing tests never call `.labels()` on `LLM_BUDGET_EXCEEDED` directly, so the wider
label set doesn't touch them; that call site is fixed in Task 3)

- [ ] **Step 6: Commit**

```bash
git add roger/metrics.py tests/test_metrics.py
git commit -m "feat: add USD-cap gauge and reason label to budget metrics"
```

---

### Task 3: layer the dollar cap into `LLM.complete`

**Files:**
- Modify: `roger/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `Settings.daily_usd_*` (Task 1), `metrics.LLM_BUDGET_EXCEEDED.labels(brain, reason)`
  (Task 2), `Store.cost_today(brain) -> float` (already exists in `roger/store.py`).
- Produces: `BudgetExceeded(brain, used, cap, *, unit="tokens")` — `unit` is `"tokens"` or `"usd"`,
  stored as `.unit` on the instance. Consumed by Task 4.

- [ ] **Step 1: Write the failing tests**

In `tests/test_llm.py`, find:

```python
async def test_budget_exceeded_before_network(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_ADMIN="a/b", DAILY_TOKENS_ADMIN="10")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        await store.add_usage("admin", 8, 5)  # 13 >= 10
        llm = LLM(Settings(), store)
        with pytest.raises(BudgetExceeded):
            await llm.complete("admin", [{"role": "user", "content": "hi"}])
    finally:
        await store.close()
```

Replace with (adds a `unit` assertion to the existing test, then two new tests directly after it):

```python
async def test_budget_exceeded_before_network(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_ADMIN="a/b", DAILY_TOKENS_ADMIN="10")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        await store.add_usage("admin", 8, 5)  # 13 >= 10
        llm = LLM(Settings(), store)
        with pytest.raises(BudgetExceeded) as exc_info:
            await llm.complete("admin", [{"role": "user", "content": "hi"}])
        assert exc_info.value.unit == "tokens"
    finally:
        await store.close()


async def test_usd_budget_exceeded_before_network(monkeypatch, tmp_path):
    # Token cap (default 150k) is nowhere near tripped — only the $ cap should fire.
    _env(monkeypatch, MODEL_ADMIN="a/b", DAILY_USD_ADMIN="1.0")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        await store.add_usage("admin", 1, 1, cost_usd=1.5)  # $1.50 >= $1.00 cap
        llm = LLM(Settings(), store)
        with pytest.raises(BudgetExceeded) as exc_info:
            await llm.complete("admin", [{"role": "user", "content": "hi"}])
        assert exc_info.value.unit == "usd"
    finally:
        await store.close()


async def test_usd_cap_does_not_trip_below_threshold(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_ADMIN="a/b", DAILY_USD_ADMIN="1.0")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        await store.add_usage("admin", 1, 1, cost_usd=0.5)  # $0.50 < $1.00 cap
        llm = LLM(Settings(), store)

        async def fake_create(**kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, cost=0.1)
            )

        monkeypatch.setattr(llm._client.chat.completions, "create", fake_create)
        await llm.complete("admin", [{"role": "user", "content": "hi"}])  # does not raise
    finally:
        await store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_llm.py -v -k "budget or usd_cap"`
Expected: FAIL — `exc_info.value.unit` raises `AttributeError` (no `unit` on `BudgetExceeded` yet);
the new `$`-cap tests never trip since `LLM.complete` doesn't check `cost_today` yet
(`test_usd_budget_exceeded_before_network` fails because no exception is raised at all)

- [ ] **Step 3: Make `BudgetExceeded` unit-aware**

In `roger/llm.py`, find:

```python
class BudgetExceeded(RuntimeError):
    def __init__(self, brain: str, used: int, cap: int) -> None:
        super().__init__(f"{brain} daily token budget exceeded ({used} >= {cap})")
        self.brain = brain
        self.used = used
        self.cap = cap
```

Replace with:

```python
class BudgetExceeded(RuntimeError):
    def __init__(self, brain: str, used: float, cap: float, *, unit: str = "tokens") -> None:
        if unit == "usd":
            message = f"{brain} daily $ budget exceeded (${used:.4f} >= ${cap:.4f})"
        else:
            message = f"{brain} daily token budget exceeded ({used} >= {cap})"
        super().__init__(message)
        self.brain = brain
        self.used = used
        self.cap = cap
        self.unit = unit
```

- [ ] **Step 4: Track the USD caps in `LLM.__init__`**

In `roger/llm.py`, find:

```python
        self._caps = {
            "admin": settings.daily_tokens_admin,
            "ambient": settings.daily_tokens_ambient,
            "digest": settings.daily_tokens_digest,
            "gigabrain": settings.daily_tokens_gigabrain,
        }
        # Opt-in OpenRouter `reasoning.effort` passthrough — only gigabrain ever sets this today.
```

Replace with:

```python
        self._caps = {
            "admin": settings.daily_tokens_admin,
            "ambient": settings.daily_tokens_ambient,
            "digest": settings.daily_tokens_digest,
            "gigabrain": settings.daily_tokens_gigabrain,
        }
        self._usd_caps = {
            "admin": settings.daily_usd_admin,
            "ambient": settings.daily_usd_ambient,
            "digest": settings.daily_usd_digest,
            "gigabrain": settings.daily_usd_gigabrain,
        }
        # Opt-in OpenRouter `reasoning.effort` passthrough — only gigabrain ever sets this today.
```

- [ ] **Step 5: Add the layered `$` check to `complete`**

In `roger/llm.py`, find:

```python
        used = await self._store.usage_today(brain)
        cap = self._caps[brain]
        if used >= cap:
            metrics.LLM_BUDGET_EXCEEDED.labels(brain).inc()
            raise BudgetExceeded(brain, used, cap)
```

Replace with:

```python
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

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_llm.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 7: Commit**

```bash
git add roger/llm.py tests/test_llm.py
git commit -m "feat: layer a dollar cap on top of the token budget gate"
```

---

### Task 4: unit-aware budget messages in the four brains

**Files:**
- Modify: `roger/brains/admin.py:167-176`
- Modify: `roger/brains/gigabrain.py:170-179`
- Modify: `roger/brains/digest.py:99-101`
- Test: `tests/test_admin.py`, `tests/test_gigabrain.py`

**Interfaces:**
- Consumes: `BudgetExceeded.unit` (Task 3).

`roger/brains/ambient.py`'s `BUDGET_LINE` ("I'm out of words for today.") is already unit-agnostic —
no change needed there.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_admin.py`:

```python
async def test_budget_exceeded_audit_detail_reflects_unit(tmp_path):
    store = await _open_store(tmp_path)
    try:
        llm = FakeLLM([BudgetExceeded("admin", 2.5, 2.0, unit="usd")])
        await admin.handle_admin_request(
            request="anything", guild=object(), actor_id=1, llm=llm, store=store
        )
        rows = await store.fetch_audit()
        assert any(r["detail"] == "daily usd cap" for r in rows)
    finally:
        await store.close()
```

Append to `tests/test_gigabrain.py`:

```python
async def test_budget_exceeded_audit_detail_reflects_unit(tmp_path):
    store = await _open_store(tmp_path)
    try:
        llm = FakeLLM([BudgetExceeded("gigabrain", 2.5, 2.0, unit="usd")])
        await gigabrain.handle_gigabrain_request(
            request="anything", guild=object(), actor_id=1, llm=llm, store=store
        )
        rows = await store.fetch_audit()
        assert any(r["detail"] == "daily usd cap" for r in rows)
    finally:
        await store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_admin.py tests/test_gigabrain.py -v -k unit`
Expected: FAIL — `AssertionError` (audit `detail` is still the hardcoded `"daily token cap"`)

- [ ] **Step 3: Fix `admin.py`**

In `roger/brains/admin.py`, find:

```python
    except BudgetExceeded:
        await store.record_audit(
            actor_id=actor_id,
            brain="admin",
            tool=None,
            args={"request": request},
            status=AuditStatus.ERROR,
            detail="daily token cap",
        )
        return "I've hit my daily token budget for admin work. Try again tomorrow."
```

Replace with:

```python
    except BudgetExceeded as exc:
        await store.record_audit(
            actor_id=actor_id,
            brain="admin",
            tool=None,
            args={"request": request},
            status=AuditStatus.ERROR,
            detail=f"daily {exc.unit} cap",
        )
        return "I've hit my daily budget for admin work. Try again tomorrow."
```

- [ ] **Step 4: Fix `gigabrain.py`**

In `roger/brains/gigabrain.py`, find:

```python
    except BudgetExceeded:
        await store.record_audit(
            actor_id=actor_id,
            brain="gigabrain",
            tool=None,
            args={"request": request},
            status=AuditStatus.ERROR,
            detail="daily token cap",
        )
        return "I've hit my daily token budget for gigabrain work. Try again tomorrow."
```

Replace with:

```python
    except BudgetExceeded as exc:
        await store.record_audit(
            actor_id=actor_id,
            brain="gigabrain",
            tool=None,
            args={"request": request},
            status=AuditStatus.ERROR,
            detail=f"daily {exc.unit} cap",
        )
        return "I've hit my daily budget for gigabrain work. Try again tomorrow."
```

- [ ] **Step 5: Fix `digest.py`**

In `roger/brains/digest.py`, find:

```python
    try:
        summary = await _summarize(entries, llm)
    except BudgetExceeded:
        log.warning("digest skipped: daily token budget hit")
        return {"status": "budget exceeded; skipped"}
```

Replace with:

```python
    try:
        summary = await _summarize(entries, llm)
    except BudgetExceeded as exc:
        log.warning("digest skipped: daily %s budget hit", exc.unit)
        return {"status": "budget exceeded; skipped"}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_admin.py tests/test_gigabrain.py tests/test_digest.py tests/test_ambient.py -v`
Expected: PASS (all tests in these four files — the pre-existing
`test_budget_exceeded_returns_polite_refusal` tests only assert `"budget" in out.lower()`, which
still holds with "daily budget" in place of "daily token budget")

- [ ] **Step 7: Commit**

```bash
git add roger/brains/admin.py roger/brains/gigabrain.py roger/brains/digest.py \
        tests/test_admin.py tests/test_gigabrain.py
git commit -m "fix: make budget-exceeded messages unit-aware, not token-only"
```

---

### Task 5: ops alert watches the dollar cap

**Files:**
- Modify: `roger/bot.py:261-268` (add `_daily_usd_caps`)
- Modify: `roger/bot.py:397-407` (`_budget_alert`)
- Modify: `roger/bot.py:714-724` (watchdog loop)
- Test: `tests/test_ops.py`

**Interfaces:**
- Consumes: `Settings.daily_usd_*` (Task 1).
- Produces: `_daily_usd_caps(settings) -> dict[str, float]` (mirrors `_daily_caps`, used by Task 6
  too). `_budget_alert` gains a keyword-only `usd_cap: float = 0.0` param — default preserves every
  existing call site and test unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ops.py`:

```python
def test_budget_alert_fires_from_usd_cap_alone():
    # tokens are nowhere near their cap (10%); the $ cap (84%) is what should trip this.
    msg = _budget_alert("gigabrain", 10_000, 100_000, 4.2, usd_cap=5.0)
    assert msg is not None
    assert "84%" in msg and "$4.2000 / $5.0000" in msg


def test_budget_alert_usd_exhausted_reads_as_exhausted():
    msg = _budget_alert("gigabrain", 1_000, 100_000, 6.0, usd_cap=5.0)
    assert msg is not None and "exhausted" in msg


def test_budget_alert_silent_when_both_caps_disabled():
    assert _budget_alert("admin", 1_000_000, 0, 999.0, usd_cap=0.0) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ops.py -v -k usd_cap`
Expected: FAIL — `TypeError: _budget_alert() got an unexpected keyword argument 'usd_cap'`

- [ ] **Step 3: Add `_daily_usd_caps`**

In `roger/bot.py`, find:

```python
def _daily_caps(settings: Settings) -> dict[str, int]:
    """Per-brain daily token caps, keyed by brain (shared by /status and the watchdog)."""
    return {
        "admin": settings.daily_tokens_admin,
        "ambient": settings.daily_tokens_ambient,
        "digest": settings.daily_tokens_digest,
        "gigabrain": settings.daily_tokens_gigabrain,
    }
```

Replace with:

```python
def _daily_caps(settings: Settings) -> dict[str, int]:
    """Per-brain daily token caps, keyed by brain (shared by /status and the watchdog)."""
    return {
        "admin": settings.daily_tokens_admin,
        "ambient": settings.daily_tokens_ambient,
        "digest": settings.daily_tokens_digest,
        "gigabrain": settings.daily_tokens_gigabrain,
    }


def _daily_usd_caps(settings: Settings) -> dict[str, float]:
    """Per-brain daily USD caps, keyed by brain (0 = disabled). Mirrors `_daily_caps`."""
    return {
        "admin": settings.daily_usd_admin,
        "ambient": settings.daily_usd_ambient,
        "digest": settings.daily_usd_digest,
        "gigabrain": settings.daily_usd_gigabrain,
    }
```

- [ ] **Step 4: Make `_budget_alert` watch both caps**

In `roger/bot.py`, find:

```python
def _budget_alert(
    brain: str, used: int, cap: int, cost: float, *, fraction: float = BUDGET_ALERT_FRACTION
) -> str | None:
    """Alert text when ``brain`` crosses ``fraction`` of its daily token cap, else None (pure)."""
    if cap <= 0 or used < fraction * cap:
        return None
    tokens = f"{used:,} / {cap:,} tokens today (${cost:.4f})"
    if used >= cap:
        return f"⚠️ **{brain} budget exhausted** — {tokens}. Calls refused until the daily reset."
    pct = round(100 * used / cap)
    return f"⚠️ **{brain} budget {pct}%** — {tokens}. Approaching the daily cap."
```

Replace with:

```python
def _budget_alert(
    brain: str,
    used: int,
    cap: int,
    cost: float,
    *,
    usd_cap: float = 0.0,
    fraction: float = BUDGET_ALERT_FRACTION,
) -> str | None:
    """Alert text once ``brain`` crosses ``fraction`` of its daily token or $ cap, else None (pure).

    The two caps are independent — whichever fraction is worse decides both whether this fires and
    the wording (exhausted vs. approaching). A disabled cap (``cap`` or ``usd_cap`` <= 0) contributes
    0 to that comparison, so it can never itself trigger an alert.
    """
    token_frac = used / cap if cap > 0 else 0.0
    usd_frac = cost / usd_cap if usd_cap > 0 else 0.0
    worst = max(token_frac, usd_frac)
    if worst < fraction:
        return None
    detail = f"{used:,} / {cap:,} tokens today" if cap > 0 else f"{used:,} tokens today"
    if usd_cap > 0:
        detail += f" · ${cost:.4f} / ${usd_cap:.4f}"
    else:
        detail += f" (${cost:.4f})"
    if worst >= 1.0:
        return f"⚠️ **{brain} budget exhausted** — {detail}. Calls refused until the daily reset."
    pct = round(100 * worst)
    return f"⚠️ **{brain} budget {pct}%** — {detail}. Approaching the daily cap."
```

- [ ] **Step 5: Wire the watchdog loop to pass the USD cap through**

In `roger/bot.py`, find:

```python
        caps = _daily_caps(self.settings)
        today = time.strftime("%Y-%m-%d")
        for brain in _BRAINS:
            message = _budget_alert(
                brain,
                await self.store.usage_today(brain),
                caps[brain],
                await self.store.cost_today(brain),
            )
            if message:
```

Replace with:

```python
        caps = _daily_caps(self.settings)
        usd_caps = _daily_usd_caps(self.settings)
        today = time.strftime("%Y-%m-%d")
        for brain in _BRAINS:
            message = _budget_alert(
                brain,
                await self.store.usage_today(brain),
                caps[brain],
                await self.store.cost_today(brain),
                usd_cap=usd_caps[brain],
            )
            if message:
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_ops.py -v`
Expected: PASS (all tests in the file — the 4 pre-existing `_budget_alert` tests never pass
`usd_cap`, so it defaults to `0.0`; verify by hand: `max(token_frac, 0.0) == token_frac`, so their
behavior is byte-for-byte unchanged)

- [ ] **Step 7: Commit**

```bash
git add roger/bot.py tests/test_ops.py
git commit -m "feat: extend the ops budget alert to watch the dollar cap"
```

---

### Task 6: show the dollar cap in `/status`

**Files:**
- Modify: `roger/bot.py:297-338` (`_format_status`)
- Modify: `roger/bot.py:341-362` (`gather_status`)
- Test: `tests/test_status.py`

**Interfaces:**
- Consumes: `_daily_usd_caps` (Task 5), `Settings.daily_usd_*` (Task 1).
- Produces: `_format_status(..., usd_caps: dict[str, float] | None = None)` — default `None` (treated
  as `{}`) keeps every existing call site working unchanged.

- [ ] **Step 1: Write the failing tests**

In `tests/test_status.py`, find:

```python
def _settings(**over):
    base = dict(
        guild_id=9,
        daily_tokens_admin=150000,
        daily_tokens_ambient=40000,
        daily_tokens_digest=30000,
        daily_tokens_gigabrain=100000,
        digest_hour=8,
        digest_channel_id=42,
        tz="UTC",
    )
    base.update(over)
    return SimpleNamespace(**base)
```

Replace with:

```python
def _settings(**over):
    base = dict(
        guild_id=9,
        daily_tokens_admin=150000,
        daily_tokens_ambient=40000,
        daily_tokens_digest=30000,
        daily_tokens_gigabrain=100000,
        daily_usd_admin=0.0,
        daily_usd_ambient=0.0,
        daily_usd_digest=0.0,
        daily_usd_gigabrain=0.0,
        digest_hour=8,
        digest_channel_id=42,
        tz="UTC",
    )
    base.update(over)
    return SimpleNamespace(**base)
```

Then append two new tests at the end of the file:

```python
def test_format_status_shows_usd_cap_when_configured():
    body = _format_status(
        guild_name="G",
        missing_perms=[],
        channel_problems=[],
        usage={"admin": 1000},
        caps={"admin": 150000},
        cost={"admin": 0.5},
        usd_caps={"admin": 2.0},
        feeds_count=0,
        recent_audit=[],
        digest_hour=8,
        digest_configured=True,
        tz="UTC",
    )
    assert "$0.5000 / $2.0000" in body


async def test_gather_status_shows_usd_cap_from_settings(tmp_path):
    store = await Store(str(tmp_path / "s.db")).open()
    try:
        await store.add_usage("admin", 10, 10, cost_usd=0.25)
        guild = _fake_guild(channels={42: _FakeChannel()})
        settings = _settings(daily_usd_admin=1.0)
        body = await gather_status(store=store, settings=settings, guild=guild)
        assert "$0.2500 / $1.0000" in body
    finally:
        await store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_status.py -v -k usd_cap`
Expected: FAIL — `TypeError: _format_status() got an unexpected keyword argument 'usd_caps'`

- [ ] **Step 3: Add `usd_caps` to `_format_status`**

In `roger/bot.py`, find:

```python
def _format_status(
    *,
    guild_name: str,
    missing_perms: list[str],
    channel_problems: list[str],
    usage: dict[str, int],
    caps: dict[str, int],
    cost: dict[str, float],
    feeds_count: int,
    recent_audit: list[dict[str, Any]],
    digest_hour: int,
    digest_configured: bool,
    tz: str,
) -> str:
    """Render the /status readout body (pure). The caller wraps it in a code block."""
    perms = "OK" if not missing_perms else "MISSING: " + ", ".join(missing_perms)
    channels = "OK" if not channel_problems else "; ".join(channel_problems)
    lines = [
        f"roger status — {guild_name}",
        f"permissions: {perms}",
        f"channels: {channels}",
        "spend today (tokens used / cap · cost):",
    ]
    total_cost = 0.0
    for brain in _BRAINS:
        spent = cost.get(brain, 0.0)
        total_cost += spent
        lines.append(
            f"  {brain:<10}{usage.get(brain, 0):>8,} / {caps.get(brain, 0):<8,}  ${spent:.4f}"
        )
    lines.append(f"  {'total':<29}  ${total_cost:.4f}")
```

Replace with:

```python
def _format_status(
    *,
    guild_name: str,
    missing_perms: list[str],
    channel_problems: list[str],
    usage: dict[str, int],
    caps: dict[str, int],
    cost: dict[str, float],
    feeds_count: int,
    recent_audit: list[dict[str, Any]],
    digest_hour: int,
    digest_configured: bool,
    tz: str,
    usd_caps: dict[str, float] | None = None,
) -> str:
    """Render the /status readout body (pure). The caller wraps it in a code block."""
    usd_caps = usd_caps or {}
    perms = "OK" if not missing_perms else "MISSING: " + ", ".join(missing_perms)
    channels = "OK" if not channel_problems else "; ".join(channel_problems)
    lines = [
        f"roger status — {guild_name}",
        f"permissions: {perms}",
        f"channels: {channels}",
        "spend today (tokens used / cap · cost):",
    ]
    total_cost = 0.0
    for brain in _BRAINS:
        spent = cost.get(brain, 0.0)
        total_cost += spent
        cost_str = f"${spent:.4f}"
        usd_cap = usd_caps.get(brain, 0.0)
        if usd_cap > 0:
            cost_str += f" / ${usd_cap:.4f}"
        lines.append(
            f"  {brain:<10}{usage.get(brain, 0):>8,} / {caps.get(brain, 0):<8,}  {cost_str}"
        )
    lines.append(f"  {'total':<29}  ${total_cost:.4f}")
```

- [ ] **Step 4: Pass `usd_caps` through `gather_status`**

In `roger/bot.py`, find:

```python
    usage = {brain: await store.usage_today(brain) for brain in _BRAINS}
    cost = {brain: await store.cost_today(brain) for brain in _BRAINS}
    caps = _daily_caps(settings)
    return _format_status(
        guild_name=guild_name,
        missing_perms=missing,
        channel_problems=channel_problems,
        usage=usage,
        caps=caps,
        cost=cost,
        feeds_count=await store.count_feeds(),
```

Replace with:

```python
    usage = {brain: await store.usage_today(brain) for brain in _BRAINS}
    cost = {brain: await store.cost_today(brain) for brain in _BRAINS}
    caps = _daily_caps(settings)
    usd_caps = _daily_usd_caps(settings)
    return _format_status(
        guild_name=guild_name,
        missing_perms=missing,
        channel_problems=channel_problems,
        usage=usage,
        caps=caps,
        cost=cost,
        usd_caps=usd_caps,
        feeds_count=await store.count_feeds(),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_status.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 6: Commit**

```bash
git add roger/bot.py tests/test_status.py
git commit -m "feat: show the dollar cap in /status when configured"
```

---

### Task 7: docs + full verification

**Files:**
- Modify: `ARCHITECTURE.md` (§2.9, §11)
- Modify: `BACKLOG.md` (1.1)

**Interfaces:** None — documentation only, no code.

- [ ] **Step 1: Update §2.9**

In `ARCHITECTURE.md`, find:

```
- **§2.9 Budgets.** A hard cap of **10 tool calls per request** (`ADMIN_MAX_TOOL_CALLS`) and **14
  model round-trips** (`ADMIN_MAX_TURNS`), plus per-brain **daily token caps** (§11) checked before
  every call. Both caps are env-overridable per deployment; hitting the tool-call cap mid-request
  logs a warning and posts once to the ops channel (if configured), and the model is told to say so
  plainly — the cap resets on the next request, not on a timer.
```

Replace with:

```
- **§2.9 Budgets.** A hard cap of **10 tool calls per request** (`ADMIN_MAX_TOOL_CALLS`) and **14
  model round-trips** (`ADMIN_MAX_TURNS`), plus per-brain **daily token caps**, optionally layered
  with a **daily USD cap** (§11), checked before every call. All caps are env-overridable per
  deployment; hitting the tool-call cap mid-request logs a warning and posts once to the ops channel
  (if configured), and the model is told to say so plainly — the cap resets on the next request, not
  on a timer.
```

- [ ] **Step 2: Update §11**

In `ARCHITECTURE.md`, find:

```
## §11 LLM layer & budgets

`roger/llm.py` wraps the OpenAI SDK pointed at OpenRouter. Per call: pick the brain's model chain
(§3), **check the daily token cap before spending** (raises `BudgetExceeded` if over), call with
automatic fallback down the chain, then **record actual usage** to `usage`. A missing/empty model
chain raises `LLMConfigError`, which callers turn into a plain "not configured" reply rather than a
crash. Real spend is additionally bounded off-box by the OpenRouter key's own credit limit.

Limits at a glance (defaults; all env-overridable):

| Control | Default |
|---|---|
| Daily tokens — admin / ambient / digest / gigabrain | 150k / 40k / 30k / 100k |
| Tool calls per admin request | 10 |
| Model round-trips per admin request | 14 |
| Tool calls / round-trips per gigabrain request | 10 / 14 |
| Gigabrain periodic check-in interval | off (0 days) |
| Ambient — per user / window / global hourly | 5 / 600s / 30 |
```

Replace with:

```
## §11 LLM layer & budgets

`roger/llm.py` wraps the OpenAI SDK pointed at OpenRouter. Per call: pick the brain's model chain
(§3), **check the daily token cap before spending** (raises `BudgetExceeded` if over), then — if a
`DAILY_USD_<BRAIN>` cap is also set — check accumulated USD spend the same way. The two caps are
layered, not either/or: the token cap always enforces, and the USD cap is an additional, optional
trip wire on top of it. That's deliberate — a provider that never reports cost
(`OPENROUTER_BASE_URL` pointed elsewhere, ADR-0009) would otherwise leave the USD cap permanently
silent, so the token cap stays the real backstop in that case. Once both checks pass, the call
proceeds with automatic fallback down the chain, then **records actual usage** to `usage`. A
missing/empty model chain raises `LLMConfigError`, which callers turn into a plain "not configured"
reply rather than a crash. Real spend is additionally bounded off-box by the OpenRouter key's own
credit limit.

Limits at a glance (defaults; all env-overridable):

| Control | Default |
|---|---|
| Daily tokens — admin / ambient / digest / gigabrain | 150k / 40k / 30k / 100k |
| Daily USD — admin / ambient / digest / gigabrain | off / off / off / off (0 = disabled) |
| Tool calls per admin request | 10 |
| Model round-trips per admin request | 14 |
| Tool calls / round-trips per gigabrain request | 10 / 14 |
| Gigabrain periodic check-in interval | off (0 days) |
| Ambient — per user / window / global hourly | 5 / 600s / 30 |
```

- [ ] **Step 3: Close out BACKLOG.md 1.1**

In `BACKLOG.md`, find:

```
### 1.1 Track spend in dollars, not just tokens — **M** — *visibility shipped; gate remains*
`llm.py` records `prompt_tokens` / `completion_tokens` per brain (`add_usage`) and the daily cap is a
raw token count. But a brain's model chain mixes models at very different prices, so a token budget
is a weak proxy for the thing that actually costs money. OpenRouter returns the real generation cost
(a `cost` field on the response `usage` object, always included now).

- [x] Add a `cost_usd` column to the `usage` table (with an idempotent migration for live DBs);
      capture the OpenRouter-reported cost per call in `LLM.complete`. *(a2689b5)*
- [x] Surface per-brain and total `$ today` in `/status`. *(a2689b5)*
- [ ] Make the daily gate dollar-denominated (env: `DAILY_USD_*`) with the token cap as the fallback
      when a provider doesn't report cost. Deferred: enforcement is a semantic change, kept out of the
      visibility commit.
```

Replace with:

```
### 1.1 Track spend in dollars, not just tokens — **M** — *shipped*
`llm.py` records `prompt_tokens` / `completion_tokens` per brain (`add_usage`) and the daily cap is a
raw token count. But a brain's model chain mixes models at very different prices, so a token budget
is a weak proxy for the thing that actually costs money. OpenRouter returns the real generation cost
(a `cost` field on the response `usage` object, always included now).

- [x] Add a `cost_usd` column to the `usage` table (with an idempotent migration for live DBs);
      capture the OpenRouter-reported cost per call in `LLM.complete`. *(a2689b5)*
- [x] Surface per-brain and total `$ today` in `/status`. *(a2689b5)*
- [x] Make the daily gate dollar-denominated (env: `DAILY_USD_*`), layered on top of the token cap
      rather than replacing it — a provider that never reports cost leaves the token cap as the real
      backstop, so nothing regresses for a non-OpenRouter host.
```

- [ ] **Step 4: Full verification**

Run: `pytest --cov=roger --cov-report=term-missing --cov-fail-under=75`
Expected: PASS, all tests green, coverage still at/above 75%

Run: `ruff check .`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add ARCHITECTURE.md BACKLOG.md
git commit -m "docs: record the dollar budget gate in ARCHITECTURE.md and BACKLOG.md"
```
