"""Boot self-report and /status readout — pure formatters plus a real-store integration."""

from types import SimpleNamespace

import discord

from roger.bot import _boot_report_line, _format_status, gather_status
from roger.store import AuditStatus, Store

# The invite integer documented in deploy/README.md — grants exactly the required scopes.
FULL_PERMS = 268454928


def test_boot_report_line_ok_is_green_and_names_the_guild():
    line = _boot_report_line("My Guild", [])
    assert "✅" in line and "roger online" in line and "My Guild" in line


def test_boot_report_line_warns_and_lists_missing_scopes():
    line = _boot_report_line("My Guild", ["Manage Channels", "Manage Roles"])
    assert "⚠️" in line and "Manage Channels, Manage Roles" in line


def test_format_status_renders_perms_usage_feeds_and_actions():
    body = _format_status(
        guild_name="Test Guild",
        missing_perms=[],
        usage={"admin": 12345, "ambient": 0, "digest": 200},
        caps={"admin": 150000, "ambient": 40000, "digest": 30000},
        feeds_count=3,
        recent_audit=[{"ts": 0, "tool": "create_channel", "status": "ok", "detail": None}],
        digest_hour=8,
        digest_configured=True,
        tz="UTC",
    )
    assert "permissions: OK" in body
    assert "12,345 / 150,000" in body
    assert "feeds: 3" in body and "08:00 UTC" in body
    assert "00:00  create_channel" in body  # epoch ts rendered in the given tz


def test_format_status_flags_missing_perms_and_unconfigured_digest():
    body = _format_status(
        guild_name="G",
        missing_perms=["Manage Roles"],
        usage={},
        caps={},
        feeds_count=0,
        recent_audit=[],
        digest_hour=8,
        digest_configured=False,
        tz="UTC",
    )
    assert "permissions: MISSING: Manage Roles" in body
    assert "digest: unconfigured" in body


def test_format_status_includes_audit_detail_when_present():
    body = _format_status(
        guild_name="G",
        missing_perms=[],
        usage={},
        caps={},
        feeds_count=0,
        recent_audit=[
            {"ts": 0, "tool": "set_permissions", "status": "denied", "detail": "owner denied"}
        ],
        digest_hour=8,
        digest_configured=True,
        tz="UTC",
    )
    assert "set_permissions" in body and "denied (owner denied)" in body


def _settings(**over):
    base = dict(
        guild_id=9,
        daily_tokens_admin=150000,
        daily_tokens_ambient=40000,
        daily_tokens_digest=30000,
        digest_hour=8,
        digest_channel_id=42,
        tz="UTC",
    )
    base.update(over)
    return SimpleNamespace(**base)


async def test_gather_status_reads_live_store(tmp_path):
    store = await Store(str(tmp_path / "s.db")).open()
    try:
        await store.add_usage("admin", 100, 50)  # 150 in+out
        await store.add_feed("http://a", "A")
        await store.record_audit(
            actor_id=1, brain="admin", tool="create_channel", args=None,
            status=AuditStatus.OK, detail=None,
        )
        guild = SimpleNamespace(
            name="Live Guild",
            me=SimpleNamespace(guild_permissions=discord.Permissions(FULL_PERMS)),
        )
        body = await gather_status(store=store, settings=_settings(), guild=guild)
        assert "Live Guild" in body
        assert "permissions: OK" in body  # the full invite set grants everything required
        assert "150 / 150,000" in body
        assert "feeds: 1" in body
        assert "create_channel" in body
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
        assert "digest: unconfigured" in body
    finally:
        await store.close()
