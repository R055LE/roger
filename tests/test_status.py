"""Boot self-report and /status readout — pure formatters plus a real-store integration."""

from types import SimpleNamespace

import discord

from roger.bot import _boot_header, _format_status, _unreachable_channels, gather_status
from roger.store import AuditStatus, Store

# The invite integer documented in deploy/README.md — grants exactly the required scopes.
FULL_PERMS = 268454928


class _FakeChannel:
    def __init__(self, name="digest", perms=None):
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


def test_boot_header_ok_is_green_and_shows_the_version():
    line = _boot_header("sha-abc1234", [], [])
    assert "✅" in line and "roger online" in line and "sha-abc1234" in line


def test_boot_header_warns_lists_missing_scopes_and_shows_version():
    line = _boot_header("dev", ["Manage Channels", "Manage Roles"], [])
    assert "⚠️" in line and "Manage Channels, Manage Roles" in line
    assert "re-invite" in line and "dev" in line


def test_boot_header_warns_on_channel_problems_even_with_full_permissions():
    line = _boot_header("dev", [], ["digest channel 42 not found"])
    assert "⚠️" in line and "digest channel 42 not found" in line


def test_unreachable_channels_flags_a_missing_channel():
    guild = _fake_guild(channels={})
    settings = SimpleNamespace(digest_channel_id=42, ops_channel_id=None, gigabrain_channel_id=None)
    problems = _unreachable_channels(guild, settings)
    assert "digest channel 42 not found" in problems[0]


def test_unreachable_channels_flags_a_channel_roger_cant_post_in():
    no_send = discord.Permissions(view_channel=True, send_messages=False)
    guild = _fake_guild(channels={99: _FakeChannel(name="checkins", perms=no_send)})
    settings = SimpleNamespace(
        digest_channel_id=None, ops_channel_id=None, gigabrain_channel_id=99
    )
    problems = _unreachable_channels(guild, settings)
    assert "gigabrain check-in channel #checkins not postable" in problems[0]


def test_unreachable_channels_flags_a_missing_spark_channel():
    guild = _fake_guild(channels={})
    settings = SimpleNamespace(
        digest_channel_id=None, ops_channel_id=None, gigabrain_channel_id=None,
        spark_channel_id=42,
    )
    problems = _unreachable_channels(guild, settings)
    assert "spark channel 42 not found" in problems[0]


def test_unreachable_channels_flags_a_non_messageable_spark_channel():
    channel = SimpleNamespace(
        name="category",
        permissions_for=lambda member: discord.Permissions(FULL_PERMS),
    )
    guild = _fake_guild(channels={42: channel})
    settings = SimpleNamespace(spark_channel_id=42)
    assert _unreachable_channels(guild, settings) == ["spark channel #category is not postable"]


def test_unreachable_channels_empty_when_nothing_configured_or_everything_reachable():
    guild = _fake_guild(channels={42: _FakeChannel()})
    settings = SimpleNamespace(digest_channel_id=42, ops_channel_id=None, gigabrain_channel_id=None)
    assert _unreachable_channels(guild, settings) == []


def test_format_status_renders_perms_usage_feeds_and_actions():
    body = _format_status(
        guild_name="Test Guild",
        missing_perms=[],
        channel_problems=[],
        usage={"admin": 12345, "ambient": 0, "digest": 200},
        caps={"admin": 150000, "ambient": 40000, "digest": 30000},
        cost={"admin": 0.0123, "ambient": 0.0, "digest": 0.002},
        feeds_count=3,
        recent_audit=[{"ts": 0, "tool": "create_channel", "status": "ok", "detail": None}],
        digest_hour=8,
        digest_configured=True,
        spark_hour=7,
        spark_configured=True,
        tz="UTC",
    )
    assert "permissions: OK" in body
    assert "channels: OK" in body
    assert "12,345 / 150,000" in body
    assert "$0.0123" in body  # per-brain cost
    assert "total" in body and "$0.0143" in body  # summed across brains
    assert "feeds: 3" in body and "digest: 08:00 UTC" in body
    assert "spark: 07:00 UTC" in body
    assert "00:00  create_channel" in body  # epoch ts rendered in the given tz


def test_format_status_flags_missing_perms_and_unconfigured_digest():
    body = _format_status(
        guild_name="G",
        missing_perms=["Manage Roles"],
        channel_problems=[],
        usage={},
        caps={},
        cost={},
        feeds_count=0,
        recent_audit=[],
        digest_hour=8,
        digest_configured=False,
        spark_hour=7,
        spark_configured=False,
        tz="UTC",
    )
    assert "permissions: MISSING: Manage Roles" in body
    assert "digest: unconfigured" in body
    assert "spark: unconfigured" in body


def test_format_status_flags_channel_problems():
    body = _format_status(
        guild_name="G",
        missing_perms=[],
        channel_problems=["gigabrain check-in channel 555 not found"],
        usage={},
        caps={},
        cost={},
        feeds_count=0,
        recent_audit=[],
        digest_hour=8,
        digest_configured=True,
        spark_hour=7,
        spark_configured=False,
        tz="UTC",
    )
    assert "channels: gigabrain check-in channel 555 not found" in body


def test_format_status_includes_audit_detail_when_present():
    body = _format_status(
        guild_name="G",
        missing_perms=[],
        channel_problems=[],
        usage={},
        caps={},
        cost={},
        feeds_count=0,
        recent_audit=[
            {"ts": 0, "tool": "set_permissions", "status": "denied", "detail": "owner denied"}
        ],
        digest_hour=8,
        digest_configured=True,
        spark_hour=7,
        spark_configured=False,
        tz="UTC",
    )
    assert "set_permissions" in body and "denied (owner denied)" in body


def _settings(**over):
    base = dict(
        guild_id=9,
        daily_tokens_admin=150000,
        daily_tokens_ambient=40000,
        daily_tokens_digest=30000,
        daily_tokens_spark=30000,
        daily_tokens_gigabrain=100000,
        daily_usd_admin=0.0,
        daily_usd_ambient=0.0,
        daily_usd_digest=0.0,
        daily_usd_spark=0.0,
        daily_usd_gigabrain=0.0,
        digest_hour=8,
        digest_channel_id=42,
        spark_hour=7,
        spark_channel_id=None,
        tz="UTC",
    )
    base.update(over)
    return SimpleNamespace(**base)


async def test_gather_status_reads_live_store(tmp_path):
    store = await Store(str(tmp_path / "s.db")).open()
    try:
        await store.add_usage("admin", 100, 50, cost_usd=0.0075)  # 150 in+out
        await store.add_usage("spark", 20, 10, cost_usd=0.001)
        await store.add_feed("http://a", "A")
        await store.record_audit(
            actor_id=1, brain="admin", tool="create_channel", args=None,
            status=AuditStatus.OK, detail=None,
        )
        guild = _fake_guild(channels={42: _FakeChannel()})
        body = await gather_status(store=store, settings=_settings(), guild=guild)
        assert "Live Guild" in body
        assert "permissions: OK" in body  # the full invite set grants everything required
        assert "channels: OK" in body  # digest_channel_id=42 resolves and is postable
        assert "150 / 150,000" in body
        assert "$0.0075" in body  # OpenRouter-reported cost surfaced from the live store
        assert "spark" in body and "30 / 30,000" in body
        assert "feeds: 1" in body
        assert "create_channel" in body
    finally:
        await store.close()


async def test_gather_status_flags_an_unreachable_configured_channel(tmp_path):
    store = await Store(str(tmp_path / "s.db")).open()
    try:
        guild = _fake_guild(channels={})  # digest_channel_id=42 won't resolve
        body = await gather_status(store=store, settings=_settings(), guild=guild)
        assert "channels: digest channel 42 not found" in body
    finally:
        await store.close()


async def test_gather_status_without_a_visible_guild(tmp_path):
    store = await Store(str(tmp_path / "s.db")).open()
    try:
        body = await gather_status(
            store=store, settings=_settings(digest_channel_id=None), guild=None
        )
        assert "roger status — 9" in body  # falls back to the guild id
        assert "permissions: OK" in body  # no guild -> nothing reported missing
        assert "channels: OK" in body  # no guild -> nothing to check either
        assert "digest: unconfigured" in body
    finally:
        await store.close()


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
        spark_hour=7,
        spark_configured=False,
        tz="UTC",
    )
    assert "$0.5000 / $2.0000" in body
    assert "ambient" in body and "$0.0000 / $" not in body  # unconfigured brain: no cap suffix


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
