"""The 'toy' tools — presence, nickname, server stats, reactions — against fakes.

Same spirit as test_executors.py: no live Discord, just enough of a fake surface to prove the
behaviour and the graceful degradation when a permission is missing.
"""

import json
from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest

from roger.store import Store
from roger.tools import executors
from roger.tools.context import ToolContext
from roger.tools.guard import GuardError
from roger.tools.schemas import (
    AddReactionArgs,
    ServerStatsArgs,
    SetNicknameArgs,
    SetPresenceArgs,
)


def _http_error(kind, status):
    """Build a real discord HTTP error without a live aiohttp response."""
    response = SimpleNamespace(status=status, reason="test")
    return kind(response, "boom")


# --------------------------------------------------------------------------- set_presence


class FakeClient:
    def __init__(self):
        self.applied = None  # (status, activity) from the last change_presence

    async def change_presence(self, *, status, activity):
        self.applied = (status, activity)


@pytest.fixture
async def presence(tmp_path):
    store = await Store(str(tmp_path / "toys.db")).open()
    client = FakeClient()
    try:
        yield SimpleNamespace(
            store=store, client=client, ctx=ToolContext(store=store, client=client)
        )
    finally:
        await store.close()


def test_presence_args_require_something():
    with pytest.raises(ValueError):
        SetPresenceArgs()  # nothing at all


def test_presence_args_verb_needs_text():
    with pytest.raises(ValueError):
        SetPresenceArgs(activity="watching")  # verb without text
    with pytest.raises(ValueError):
        SetPresenceArgs(text="the logs")  # text without a verb
    with pytest.raises(ValueError):
        SetPresenceArgs(activity="none", text="x")  # clear + text is contradictory


async def test_set_presence_status_only_leaves_activity_unset(presence):
    out = await executors.set_presence(None, SetPresenceArgs(status="idle"), presence.ctx)
    assert out == {"status": "idle", "activity": None, "text": None}
    status, activity = presence.client.applied
    assert status is discord.Status.idle and activity is None
    assert json.loads(await presence.store.get_meta("presence"))["status"] == "idle"


async def test_set_presence_merges_over_stored_state(presence):
    await executors.set_presence(None, SetPresenceArgs(status="dnd"), presence.ctx)
    # Setting only the activity must keep the previously stored status.
    out = await executors.set_presence(
        None, SetPresenceArgs(activity="watching", text="the logs"), presence.ctx
    )
    assert out == {"status": "dnd", "activity": "watching", "text": "the logs"}
    status, activity = presence.client.applied
    assert status is discord.Status.dnd
    assert activity.type is discord.ActivityType.watching and activity.name == "the logs"


async def test_set_presence_none_clears_activity_but_keeps_status(presence):
    await executors.set_presence(
        None, SetPresenceArgs(status="online", activity="playing", text="chess"), presence.ctx
    )
    out = await executors.set_presence(None, SetPresenceArgs(activity="none"), presence.ctx)
    assert out == {"status": "online", "activity": None, "text": None}
    assert presence.client.applied[1] is None  # activity cleared on the gateway


async def test_set_presence_without_context_raises():
    with pytest.raises(GuardError):
        await executors.set_presence(None, SetPresenceArgs(status="idle"), None)


async def test_apply_presence_reapplies_stored_outfit(presence):
    # What bot.py does on every on_ready: push a stored dict straight onto the gateway.
    await executors.apply_presence(
        presence.client, {"status": "idle", "activity": "listening", "text": "the void"}
    )
    status, activity = presence.client.applied
    assert status is discord.Status.idle
    assert activity.type is discord.ActivityType.listening and activity.name == "the void"


# --------------------------------------------------------------------------- set_nickname


class FakeMe:
    def __init__(self):
        self.nick = "Roger"
        self.forbid = False

    async def edit(self, *, nick):
        if self.forbid:
            raise _http_error(discord.Forbidden, 403)
        self.nick = nick


class NickGuild:
    def __init__(self):
        self.me = FakeMe()


async def test_set_nickname_sets_and_reports():
    guild = NickGuild()
    out = await executors.set_nickname(guild, SetNicknameArgs(nickname="Rodge"))
    assert out == {"nickname": "Rodge", "reset": False}
    assert guild.me.nick == "Rodge"


async def test_set_nickname_empty_resets():
    guild = NickGuild()
    out = await executors.set_nickname(guild, SetNicknameArgs(nickname="  "))
    assert out == {"nickname": None, "reset": True}
    assert guild.me.nick is None


async def test_set_nickname_forbidden_is_a_clean_guard_error():
    guild = NickGuild()
    guild.me.forbid = True
    with pytest.raises(GuardError, match="Change Nickname"):
        await executors.set_nickname(guild, SetNicknameArgs(nickname="Rodge"))


# --------------------------------------------------------------------------- server_stats


class StatsGuild:
    name = "Test Server"
    member_count = 42
    text_channels = [object(), object(), object()]
    voice_channels = [object()]
    categories = [object(), object()]
    stage_channels = []
    forums = []
    roles = [object(), object(), object()]  # 3 incl @everyone → reports 2
    emojis = [object(), object()]
    premium_tier = 2
    premium_subscription_count = 7
    created_at = discord.utils.utcnow() - timedelta(days=365)


async def test_server_stats_reads_from_cache():
    out = await executors.server_stats(StatsGuild(), ServerStatsArgs())
    assert out["members"] == 42
    assert out["channels"] == {
        "text": 3, "voice": 1, "categories": 2, "stage": 0, "forums": 0
    }
    assert out["roles"] == 2  # @everyone excluded
    assert out["custom_emoji"] == 2
    assert out["boost_tier"] == 2 and out["boosts"] == 7
    assert out["age_days"] >= 364  # ~a year, allowing for clock slack


# --------------------------------------------------------------------------- add_reaction


class FakeEmoji:
    def __init__(self, name, eid):
        self.name = name
        self.id = eid

    def __str__(self):
        return f"<:{self.name}:{self.id}>"


class FakePartial:
    def __init__(self, channel, message_id):
        self._channel = channel
        self._id = message_id

    async def add_reaction(self, emoji):
        if self._channel.react_error is not None:
            raise self._channel.react_error
        self._channel.reactions.append((self._id, str(emoji)))


class ReactChannel:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.category = None
        self.reactions = []
        self.react_error = None

    def get_partial_message(self, message_id):
        return FakePartial(self, message_id)


class ReactGuild:
    def __init__(self, guild_id=999):
        self.id = guild_id
        self.chan = ReactChannel(555, "general")
        self.voice = ReactChannel(556, "Lounge")  # resolves as voice via the buckets below
        self.text_channels = [self.chan]
        self.voice_channels = [self.voice]
        self.categories = []
        self.emojis = [FakeEmoji("party", 42)]

    def get_emoji(self, eid):
        return next((e for e in self.emojis if e.id == eid), None)


async def test_add_reaction_via_message_link_unicode():
    guild = ReactGuild()
    out = await executors.add_reaction(
        guild,
        AddReactionArgs(message="https://discord.com/channels/999/555/1234", emoji="👍"),
    )
    assert out == {"reacted": True, "channel": "general", "message_id": 1234, "emoji": "👍"}
    assert guild.chan.reactions == [(1234, "👍")]


async def test_add_reaction_via_bare_id_and_channel():
    guild = ReactGuild()
    out = await executors.add_reaction(
        guild, AddReactionArgs(message="1234", emoji="👍", channel="general")
    )
    assert out["message_id"] == 1234 and out["channel"] == "general"


async def test_add_reaction_resolves_custom_emoji_by_name():
    guild = ReactGuild()
    out = await executors.add_reaction(
        guild, AddReactionArgs(message="1234", emoji=":party:", channel="general")
    )
    assert out["emoji"] == "<:party:42>"


async def test_add_reaction_unknown_custom_emoji_errors():
    guild = ReactGuild()
    with pytest.raises(GuardError, match="no custom emoji"):
        await executors.add_reaction(
            guild, AddReactionArgs(message="1234", emoji=":ghost:", channel="general")
        )


async def test_add_reaction_bare_id_needs_channel():
    guild = ReactGuild()
    with pytest.raises(GuardError, match="needs the channel"):
        await executors.add_reaction(guild, AddReactionArgs(message="1234", emoji="👍"))


async def test_add_reaction_rejects_foreign_link():
    guild = ReactGuild()
    with pytest.raises(GuardError, match="different server"):
        await executors.add_reaction(
            guild,
            AddReactionArgs(message="https://discord.com/channels/111/555/1234", emoji="👍"),
        )


async def test_add_reaction_rejects_non_text_channel():
    guild = ReactGuild()
    with pytest.raises(GuardError, match="text channel"):
        await executors.add_reaction(
            guild, AddReactionArgs(message="1234", emoji="👍", channel="Lounge")
        )


async def test_add_reaction_forbidden_names_the_permissions():
    guild = ReactGuild()
    guild.chan.react_error = _http_error(discord.Forbidden, 403)
    with pytest.raises(GuardError, match="Add Reactions"):
        await executors.add_reaction(
            guild, AddReactionArgs(message="1234", emoji="👍", channel="general")
        )


async def test_add_reaction_missing_message_is_a_clean_error():
    guild = ReactGuild()
    guild.chan.react_error = _http_error(discord.NotFound, 404)
    with pytest.raises(GuardError, match="no message"):
        await executors.add_reaction(
            guild, AddReactionArgs(message="1234", emoji="👍", channel="general")
        )
