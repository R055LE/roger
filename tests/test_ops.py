"""Ops-channel alerting — the dedupe notifier and the pure alert-decision helpers (backlog 1.2)."""

from roger.bot import OpsNotifier, _budget_alert, _digest_problem


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


def test_digest_problem_none_for_success_statuses():
    assert _digest_problem("posted") is None
    assert _digest_problem("no new items") is None


def test_digest_problem_flags_failures():
    assert _digest_problem("budget exceeded; skipped") == "budget exceeded; skipped"
    assert _digest_problem("digest channel 42 not found") is not None
    assert _digest_problem("digest brain not configured (no models)") is not None
