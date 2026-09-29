"""Boot self-report and /status readout for the active brains."""

from types import SimpleNamespace

import discord

from roger.bot import _boot_header, _format_status, _unreachable_channels, gather_status
from roger.store import AuditStatus, Store

FULL_PERMS = 268454928


class _FakeChannel:
    def __init__(self, name="news", perms=None):
        self.name = name
        self._perms = perms if perms is not None else discord.Permissions(FULL_PERMS)

    def permissions_for(self, member):
        return self._perms

    async def send(self, **kwargs):
        pass


def _fake_guild(name="Live Guild", channels=None, perms=FULL_PERMS):
    channels = channels or {}
    return SimpleNamespace(
        name=name,
        me=SimpleNamespace(guild_permissions=discord.Permissions(perms)),
        get_channel=lambda cid: channels.get(cid),
    )


def _settings(**over):
    base = dict(
        guild_id=9,
        daily_tokens_admin=150000,
        daily_tokens_ambient=40000,
        daily_tokens_curated=30000,
        daily_tokens_gigabrain=100000,
        daily_usd_admin=0.0,
        daily_usd_ambient=0.0,
        daily_usd_curated=0.0,
        daily_usd_gigabrain=0.0,
        curated_hour=7,
        curated_channel_id=42,
        ops_channel_id=None,
        gigabrain_channel_id=None,
        tz="UTC",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_boot_header_reports_health_and_configuration():
    assert "✅" in _boot_header("sha-abc1234", [], [], [])
    warning = _boot_header("dev", ["Manage Roles"], ["curated channel 42 not found"], [])
    assert "⚠️" in warning
    assert "Manage Roles" in warning
    assert "curated channel 42 not found" in warning
    assert "re-invite" in warning


def test_unreachable_curated_channel_reports_missing_destination_and_permissions():
    assert _unreachable_channels(_fake_guild(), _settings()) == [
        "curated channel 42 not found"
    ]
    no_embeds = discord.Permissions(view_channel=True, send_messages=True)
    guild = _fake_guild(channels={42: _FakeChannel(perms=no_embeds)})
    assert _unreachable_channels(guild, _settings()) == [
        "curated channel #news not postable (missing Embed Links)"
    ]


def test_unreachable_channels_checks_other_brains_and_non_messageable_destinations():
    no_send = discord.Permissions(view_channel=True, send_messages=False)
    guild = _fake_guild(channels={99: _FakeChannel(name="checkins", perms=no_send)})
    assert "gigabrain check-in channel #checkins not postable" in _unreachable_channels(
        guild, _settings(curated_channel_id=None, gigabrain_channel_id=99)
    )[0]
    category = SimpleNamespace(
        name="category", permissions_for=lambda member: discord.Permissions(FULL_PERMS)
    )
    guild = _fake_guild(channels={42: category})
    assert _unreachable_channels(guild, _settings()) == [
        "curated channel #category is not postable"
    ]


def test_format_status_shows_curated_schedule_spend_and_actions():
    body = _format_status(
        guild_name="Test Guild",
        missing_perms=[],
        channel_problems=[],
        usage={"admin": 12345, "curated": 200},
        caps={"admin": 150000, "curated": 30000},
        cost={"admin": 0.0123, "curated": 0.002},
        usd_caps={"admin": 2.0},
        recent_audit=[{"ts": 0, "tool": "create_channel", "status": "ok", "detail": None}],
        curated_hour=7,
        curated_configured=True,
        tz="UTC",
    )
    assert "permissions: OK" in body and "channels: OK" in body
    assert "12,345 / 150,000" in body
    assert "$0.0123 / $2.0000" in body
    assert "total" in body and "$0.0143" in body
    assert "curated: 07:00 UTC" in body
    assert "00:00  create_channel" in body
    assert "digest:" not in body and "spark:" not in body


def test_format_status_flags_missing_perms_and_disabled_curated_job():
    body = _format_status(
        guild_name="G",
        missing_perms=["Manage Roles"],
        channel_problems=[],
        usage={},
        caps={},
        cost={},
        recent_audit=[],
        curated_configured=False,
        tz="UTC",
    )
    assert "permissions: MISSING: Manage Roles" in body
    assert "curated: unconfigured" in body


async def test_gather_status_reads_active_spend_and_retains_legacy_usage(tmp_path):
    path = tmp_path / "s.db"
    store = await Store(str(path)).open()
    try:
        await store.add_usage("admin", 100, 50, cost_usd=0.0075)
        await store.add_usage("digest", 20, 10, cost_usd=0.001)
        await store.record_audit(
            actor_id=1, brain="admin", tool="create_channel", args=None,
            status=AuditStatus.OK, detail=None,
        )
        guild = _fake_guild(channels={42: _FakeChannel()})
        body = await gather_status(store=store, settings=_settings(), guild=guild)
        assert "Live Guild" in body and "channels: OK" in body
        assert "150 / 150,000" in body and "$0.0075" in body
        assert "curated: 07:00 UTC" in body and "create_channel" in body
        assert await store.usage_today("digest") == 30
    finally:
        await store.close()

    reopened = await Store(str(path)).open()
    try:
        assert await reopened.usage_today("digest") == 30
    finally:
        await reopened.close()


async def test_gather_status_reports_channel_and_guild_state(tmp_path):
    store = await Store(str(tmp_path / "s.db")).open()
    try:
        body = await gather_status(store=store, settings=_settings(), guild=_fake_guild())
        assert "channels: curated channel 42 not found" in body
        body = await gather_status(
            store=store, settings=_settings(curated_channel_id=None), guild=None
        )
        assert "roger status — 9" in body
        assert "curated: unconfigured" in body
    finally:
        await store.close()
