"""Ops-channel alerting — the dedupe notifier and the pure alert-decision helpers (backlog 1.2)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from roger import bot
from roger.bot import (
    OpsNotifier,
    RogerClient,
    _budget_alert,
    _curated_problem,
    _gigabrain_problem,
)
from roger.config import Settings
from roger.tools.schemas import REGISTRY


class _FakeClock:
    """A hand-cranked monotonic clock so cooldowns are tested without real time."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_legacy_news_triggers_are_retired():
    assert not any(
        hasattr(RogerClient, name)
        for name in ("_digest_loop", "_spark_loop", "_personal_digest_loop")
    )
    assert "run_digest" not in REGISTRY and "run_spark" not in REGISTRY
    assert "preview_curated" in REGISTRY


async def test_curated_schedule_stays_off_without_a_channel(monkeypatch):
    for key, value in {
        "DISCORD_TOKEN": "x", "OPENROUTER_API_KEY": "y", "OWNER_ID": "1", "GUILD_ID": "2",
        "METRICS_PORT": "0", "GIGABRAIN_INTERVAL_DAYS": "0",
    }.items():
        monkeypatch.setenv(key, value)
    settings = Settings()
    assert settings.curated_channel_id is None
    client = RogerClient(settings, store=object(), llm=object())
    monkeypatch.setattr(bot, "_register_commands", lambda _: None)
    monkeypatch.setattr(client.tree, "sync", AsyncMock())
    monkeypatch.setattr(client, "_maybe_prune", AsyncMock())
    monkeypatch.setattr(client._heartbeat, "start", Mock())

    await client.setup_hook()

    assert not client._curated_loop.is_running()


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


def test_gigabrain_problem_none_for_success_and_self_gated_statuses():
    assert _gigabrain_problem("delivered") is None
    assert _gigabrain_problem("not due yet") is None
    assert _gigabrain_problem("periodic suggestions not configured") is None


def test_gigabrain_problem_flags_failures():
    assert _gigabrain_problem("DM failed; suggestion not delivered") is not None
    assert _gigabrain_problem("guild 9 not visible") is not None
    assert _gigabrain_problem("error running suggestion") is not None


def test_curated_quiet_day_is_ok_but_uncertain_delivery_alerts():
    assert _curated_problem("no post-worthy items") is None
    assert _curated_problem("posted") is None
    assert _curated_problem("already posted") is None
    assert _curated_problem("delivery uncertain; manual check required") is not None


async def test_scheduled_curated_recovers_after_an_unexpected_tick_error(monkeypatch):
    alerts = []

    class Ops:
        async def alert(self, *args, **kwargs):
            alerts.append((args, kwargs))

    outcomes = iter([RuntimeError("boom"), {"status": "no post-worthy items"}])

    async def job(**kwargs):
        value = next(outcomes)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(bot, "run_curated_job", job)
    client = SimpleNamespace(settings=object(), llm=object(), store=object(), _ops=Ops())
    await RogerClient._run_scheduled_curated(client)
    await RogerClient._run_scheduled_curated(client)
    assert len(alerts) == 1
    assert "unexpected error" in alerts[0][0][1]
