"""Ops-channel alerting — the dedupe notifier and the pure alert-decision helpers (backlog 1.2)."""

import json
from types import SimpleNamespace

from roger import bot
from roger.bot import (
    DIGEST_LAST_ATTEMPT_META_KEY,
    OpsNotifier,
    RogerClient,
    _budget_alert,
    _digest_problem,
    _gigabrain_problem,
    _personal_digest_problem,
    _spark_problem,
)
from roger.store import Store


class _FakeClock:
    """A hand-cranked monotonic clock so cooldowns are tested without real time."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def test_notifier_dedupes_within_cooldown():
    sent: list[str] = []

    async def send(message: str) -> None:
        sent.append(message)

    clock = _FakeClock()
    ops = OpsNotifier(send, clock=clock)

    assert await ops.alert("k", "first", cooldown_s=60) is True
    assert await ops.alert("k", "second", cooldown_s=60) is False  # suppressed inside the cooldown
    clock.t += 61
    assert await ops.alert("k", "third", cooldown_s=60) is True  # cooldown elapsed → fires again
    assert sent == ["first", "third"]


async def test_notifier_keys_are_independent():
    sent: list[str] = []

    async def send(message: str) -> None:
        sent.append(message)

    ops = OpsNotifier(send, clock=_FakeClock())
    assert await ops.alert("a", "A", cooldown_s=60) is True
    assert await ops.alert("b", "B", cooldown_s=60) is True  # a different key is never suppressed
    assert sent == ["A", "B"]


def test_budget_alert_silent_below_threshold():
    assert _budget_alert("admin", 100_000, 150_000, 0.0) is None  # ~67% < 80%


def test_budget_alert_fires_at_threshold_and_quotes_cost():
    msg = _budget_alert("admin", 120_000, 150_000, 0.0842)  # exactly 80%
    assert msg is not None
    assert "80%" in msg and "$0.0842" in msg


def test_budget_alert_over_cap_reads_as_exhausted():
    msg = _budget_alert("admin", 151_479, 150_000, 0.0)
    assert msg is not None and "exhausted" in msg


def test_budget_alert_ignores_zero_or_negative_cap():
    assert _budget_alert("admin", 5, 0, 0.0) is None


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


def test_digest_problem_none_for_success_statuses():
    assert _digest_problem("posted") is None
    assert _digest_problem("no new items") is None


def test_digest_problem_flags_failures():
    assert _digest_problem("budget exceeded; skipped") == "budget exceeded; skipped"
    assert _digest_problem("digest channel 42 not found") is not None
    assert _digest_problem("digest brain not configured (no models)") is not None


def test_personal_digest_problem_none_for_success_statuses():
    assert _personal_digest_problem("posted") is None
    assert _personal_digest_problem("no new items") is None
    assert _personal_digest_problem("personal digest not configured (no feeds)") is None


def test_personal_digest_problem_flags_failures():
    assert _personal_digest_problem("DM failed; digest not delivered") is not None
    assert _personal_digest_problem("budget exceeded; skipped") is not None


def test_gigabrain_problem_none_for_success_and_self_gated_statuses():
    assert _gigabrain_problem("delivered") is None
    assert _gigabrain_problem("not due yet") is None
    assert _gigabrain_problem("periodic suggestions not configured") is None


def test_gigabrain_problem_flags_failures():
    assert _gigabrain_problem("DM failed; suggestion not delivered") is not None
    assert _gigabrain_problem("guild 9 not visible") is not None
    assert _gigabrain_problem("error running suggestion") is not None


def test_spark_problem_none_for_success_statuses():
    assert _spark_problem("posted") is None
    assert _spark_problem("no new items") is None


def test_spark_problem_flags_failures():
    assert _spark_problem("budget exceeded; skipped") is not None
    assert _spark_problem("spark channel 42 not found") is not None
    assert _spark_problem("spark brain not configured (no models)") is not None
    assert _spark_problem("unparseable response; skipped") is not None
    assert _spark_problem("delivery failed; not posted") is not None
    assert _spark_problem("spark not configured (SPARK_CHANNEL_ID unset)") is not None


async def test_scheduled_digest_records_failure_alerts_and_runs_again(tmp_path, monkeypatch):
    store = await Store(str(tmp_path / "s.db")).open()
    alerts = []

    class Ops:
        async def alert(self, *args, **kwargs):
            alerts.append((args, kwargs))

    results = iter([{"status": "delivery failed"}, {"status": "posted"}])

    async def run_digest_job(**kwargs):
        return next(results)

    monkeypatch.setattr(bot, "run_digest_job", run_digest_job)
    client = SimpleNamespace(settings=object(), llm=object(), store=store, _ops=Ops())
    try:
        await RogerClient._run_scheduled_digest(client)
        first = json.loads(await store.get_meta(DIGEST_LAST_ATTEMPT_META_KEY))
        assert set(first) == {"timestamp", "result"}
        assert first["result"] == "failure" and isinstance(first["timestamp"], float)
        assert len(alerts) == 1 and "delivery failed" in alerts[0][0][1]

        await RogerClient._run_scheduled_digest(client)
        assert json.loads(await store.get_meta(DIGEST_LAST_ATTEMPT_META_KEY))["result"] == "success"
    finally:
        await store.close()


async def test_scheduled_digest_contains_unexpected_job_error(tmp_path, monkeypatch, caplog):
    store = await Store(str(tmp_path / "s.db")).open()
    alerts = []

    class Ops:
        async def alert(self, *args, **kwargs):
            alerts.append((args, kwargs))

    async def run_digest_job(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(bot, "run_digest_job", run_digest_job)
    client = SimpleNamespace(settings=object(), llm=object(), store=store, _ops=Ops())
    try:
        await RogerClient._run_scheduled_digest(client)
        assert json.loads(await store.get_meta(DIGEST_LAST_ATTEMPT_META_KEY))["result"] == "failure"
        assert len(alerts) == 1
        assert any(record.exc_info for record in caplog.records)
    finally:
        await store.close()
