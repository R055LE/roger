"""Ambient brain — rate limiter + chat loop with a fake LLM and real temp store."""

from types import SimpleNamespace

from roger.brains import ambient
from roger.brains.ambient import BUDGET_LINE, RATE_LIMIT_LINE, AmbientLimiter, handle_ambient
from roger.llm import BudgetExceeded
from roger.store import Store


def _resp(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


class FakeLLM:
    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    async def complete(self, brain, messages, tools=None):
        self.calls += 1
        assert tools is None  # ambient never gets tools
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# --- limiter ---


def test_limiter_allows_then_notifies_then_silences():
    lim = AmbientLimiter(per_user=2, window_s=600, global_hourly=100)
    assert lim.check(1) == "ok"
    assert lim.check(1) == "ok"
    assert lim.check(1) == "notify"  # 3rd in window -> canned line once
    assert lim.check(1) == "silent"  # already notified


def test_limiter_tracks_users_independently():
    lim = AmbientLimiter(per_user=1, window_s=600, global_hourly=100)
    assert lim.check(1) == "ok"
    assert lim.check(2) == "ok"


def test_limiter_global_cap():
    lim = AmbientLimiter(per_user=100, window_s=600, global_hourly=2)
    assert lim.check(1) == "ok"
    assert lim.check(2) == "ok"
    assert lim.check(3) == "notify"  # global hourly cap hit


def test_limiter_window_resets(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ambient.time, "monotonic", lambda: clock[0])
    lim = AmbientLimiter(per_user=1, window_s=10, global_hourly=100)
    assert lim.check(1) == "ok"
    assert lim.check(1) == "notify"
    clock[0] += 11  # window elapsed
    assert lim.check(1) == "ok"


def test_limiter_notifies_on_low_uptime_host(monkeypatch):
    # Freshly-booted host: monotonic() < window_s. The first over-limit call must still notify
    # (regression: a 0.0 sentinel made `now - last_notified` read as "already notified").
    monkeypatch.setattr(ambient.time, "monotonic", lambda: 5.0)
    lim = AmbientLimiter(per_user=1, window_s=600, global_hourly=100)
    assert lim.check(1) == "ok"
    assert lim.check(1) == "notify"


# --- handle_ambient ---


async def _store(tmp_path):
    return await Store(str(tmp_path / "amb.db")).open()


async def test_replies_and_records_both_sides(tmp_path):
    store = await _store(tmp_path)
    try:
        llm = FakeLLM([_resp("Beep. Hello.")])
        out = await handle_ambient(
            content="hi",
            user_id=7,
            channel_id=9,
            llm=llm,
            store=store,
            limiter=AmbientLimiter(5, 600, 30),
        )
        assert out == "Beep. Hello."
        rows = await store.recent_ambient(7, 9)
        assert [r["role"] for r in rows] == ["user", "bot"]
        assert rows[0]["content"] == "hi"
        assert rows[1]["content"] == "Beep. Hello."
    finally:
        await store.close()


async def test_budget_returns_canned_and_logs_nothing(tmp_path):
    store = await _store(tmp_path)
    try:
        llm = FakeLLM([BudgetExceeded("ambient", 100, 50)])
        out = await handle_ambient(
            content="hi",
            user_id=7,
            channel_id=9,
            llm=llm,
            store=store,
            limiter=AmbientLimiter(5, 600, 30),
        )
        assert out == BUDGET_LINE
        assert await store.recent_ambient(7, 9) == []
    finally:
        await store.close()


async def test_rate_limited_notifies_then_silences(tmp_path):
    store = await _store(tmp_path)
    try:
        limiter = AmbientLimiter(per_user=1, window_s=600, global_hourly=30)
        llm = FakeLLM([_resp("one")])
        kw = dict(user_id=7, channel_id=9, llm=llm, store=store, limiter=limiter)
        assert await handle_ambient(content="a", **kw) == "one"
        assert await handle_ambient(content="b", **kw) == RATE_LIMIT_LINE
        assert await handle_ambient(content="c", **kw) is None
        assert llm.calls == 1  # model only hit for the first message
    finally:
        await store.close()
