"""Executor mutations against a fake guild — verifies the security invariants structurally."""

from types import SimpleNamespace

import discord
import pytest

from roger.store import Store
from roger.tools import executors, schemas
from roger.tools.context import ToolContext
from roger.tools.guard import GuardError
from roger.tools.schemas import (
    AddFeedArgs,
    ChannelGrant,
    CreateChannelArgs,
    CreateRoleArgs,
    EditChannelArgs,
    ListFeedsArgs,
    Overwrite,
    PostMessageArgs,
    RemoveFeedArgs,
    SetPermissionsArgs,
    SuggestFeedsArgs,
)


class FakeRole:
    def __init__(self, role_id, name, permissions_value=0):
        self.id = role_id
        self.name = name
        self.permissions = SimpleNamespace(value=permissions_value)


class FakeChannel:
    """A text/voice/category channel that records edits and (for text) sent messages."""

    def __init__(self, cid, name, *, category=None, topic=None):
        self.id = cid
        self.name = name
        self.category = category
        self.topic = topic
        self.edited = None
        self.sent = []
        self.perm_calls = []  # (target, overwrite) from set_permissions

    async def edit(self, **changes):
        self.edited = changes
        for key, value in changes.items():
            setattr(self, key, value)

    async def send(self, content, allowed_mentions=None):
        self.sent.append(SimpleNamespace(content=content, allowed_mentions=allowed_mentions))

    async def set_permissions(self, target, *, overwrite):
        self.perm_calls.append((target, overwrite))


class FakeGuild:
    def __init__(self):
        self.roles = [FakeRole(0, "@everyone")]
        self.categories = []
        self.text_channels = []
        self.voice_channels = []
        self._next_id = 1000
        self.last_overwrites = None
        self.me = FakeRole(1, "Roger")  # the bot's own member (guild.me); hashable overwrite key

    @property
    def default_role(self):
        return self.roles[0]

    def get_channel(self, channel_id):
        for channel in (*self.categories, *self.text_channels, *self.voice_channels):
            if channel.id == channel_id:
                return channel
        return None

    def get_role(self, role_id):
        for role in self.roles:
            if role.id == role_id:
                return role
        return None

    def add_role(self, name):
        role = FakeRole(self._id(), name)
        self.roles.append(role)
        return role

    async def create_voice_channel(self, *, name, category, overwrites):
        self.last_overwrites = overwrites
        channel = FakeChannel(self._id(), name, category=category)
        self.voice_channels.append(channel)
        return channel

    def add_text(self, name, *, category=None, topic=None):
        channel = FakeChannel(self._id(), name, category=category, topic=topic)
        self.text_channels.append(channel)
        return channel

    def add_category(self, name):
        category = FakeChannel(self._id(), name)
        self.categories.append(category)
        return category

    def _id(self):
        self._next_id += 1
        return self._next_id

    async def create_role(self, *, name, permissions, hoist, mentionable, color=None):
        role = FakeRole(self._id(), name, permissions.value)
        self.roles.append(role)
        return role

    async def create_text_channel(self, *, name, category, topic, overwrites):
        self.last_overwrites = overwrites
        channel = FakeChannel(self._id(), name, category=category, topic=topic)
        self.text_channels.append(channel)
        return channel

    async def create_category(self, *, name, overwrites=None):
        self.last_overwrites = overwrites
        category = FakeChannel(self._id(), name)
        self.categories.append(category)
        return category


async def test_create_role_always_zero_permissions():
    guild = FakeGuild()
    result = await executors.create_role(
        guild, CreateRoleArgs(name="DJs", color="#00FF00", hoist=True)
    )
    assert result["permissions"] == 0  # invariant §2.6
    assert result["name"] == "DJs"


async def test_create_role_rejects_duplicate():
    guild = FakeGuild()
    await executors.create_role(guild, CreateRoleArgs(name="DJs"))
    with pytest.raises(GuardError):
        await executors.create_role(guild, CreateRoleArgs(name="djs"))


async def test_create_readonly_text_channel_denies_send_for_everyone():
    guild = FakeGuild()
    result = await executors.create_channel(
        guild, CreateChannelArgs(name="Podcast Room", kind="text", read_only=True)
    )
    assert result["name"] == "podcast-room"
    assert result["read_only"] is True
    overwrite = guild.last_overwrites[guild.default_role]
    assert isinstance(overwrite, discord.PermissionOverwrite)
    assert overwrite.send_messages is False
    # ...but Roger keeps its own access, or it would lock itself out of the channel it just made.
    mine = guild.last_overwrites[guild.me]
    assert mine.view_channel is True and mine.send_messages is True


async def test_create_plain_text_channel_has_no_overwrites():
    guild = FakeGuild()
    await executors.create_channel(guild, CreateChannelArgs(name="general", kind="text"))
    assert guild.last_overwrites == {}


async def test_create_category_cannot_be_nested():
    guild = FakeGuild()
    with pytest.raises(GuardError):
        await executors.create_channel(
            guild, CreateChannelArgs(name="Media", kind="category", category="Other")
        )


async def test_create_private_text_hides_from_everyone_and_keeps_bot_access():
    guild = FakeGuild()
    result = await executors.create_channel(
        guild, CreateChannelArgs(name="staff", kind="text", private=True)
    )
    assert result["private"] is True
    overwrites = guild.last_overwrites
    assert overwrites[guild.default_role].view_channel is False  # hidden from @everyone
    mine = overwrites[guild.me]
    assert mine.view_channel is True and mine.send_messages is True  # Roger keeps its own access


async def test_create_private_voice_channel_hides_from_everyone():
    guild = FakeGuild()
    result = await executors.create_channel(
        guild, CreateChannelArgs(name="War Room", kind="voice", private=True)
    )
    assert result["created"] == "voice" and result["private"] is True
    assert guild.last_overwrites[guild.default_role].view_channel is False


async def test_create_channel_grant_allows_a_role_at_creation():
    guild = FakeGuild()
    djs = guild.add_role("DJs")
    result = await executors.create_channel(
        guild,
        CreateChannelArgs(
            name="podcast",
            kind="text",
            read_only=True,
            grants=[ChannelGrant(role="DJs", allow=["send_messages"])],
        ),
    )
    assert result["grants"] == ["DJs"]
    assert guild.last_overwrites[djs].send_messages is True  # DJs may post...
    assert guild.last_overwrites[guild.default_role].send_messages is False  # ...@everyone can't


async def test_create_voice_rejects_read_only():
    guild = FakeGuild()
    with pytest.raises(GuardError):
        await executors.create_channel(
            guild, CreateChannelArgs(name="Lounge", kind="voice", read_only=True)
        )


async def test_create_private_category_hides_and_keeps_bot_access():
    guild = FakeGuild()
    result = await executors.create_channel(
        guild, CreateChannelArgs(name="Admin", kind="category", private=True)
    )
    assert result["created"] == "category" and result["private"] is True
    assert guild.last_overwrites[guild.default_role].view_channel is False
    mine = guild.last_overwrites[guild.me]
    assert mine.view_channel is True and mine.send_messages is True


async def test_create_category_with_grants_lets_a_role_in():
    guild = FakeGuild()
    admins = guild.add_role("Admins")
    result = await executors.create_channel(
        guild,
        CreateChannelArgs(
            name="Staff",
            kind="category",
            private=True,
            grants=[ChannelGrant(role="Admins", allow=["view_channel"])],
        ),
    )
    assert result["grants"] == ["Admins"]
    assert guild.last_overwrites[admins].view_channel is True


async def test_create_category_rejects_read_only():
    guild = FakeGuild()
    with pytest.raises(GuardError):
        await executors.create_channel(
            guild, CreateChannelArgs(name="Media", kind="category", read_only=True)
        )


async def test_set_permissions_can_target_a_category():
    guild = FakeGuild()
    category = guild.add_category("Admin")
    result = await executors.set_permissions(
        guild,
        SetPermissionsArgs(
            channel="Admin", overwrites=[Overwrite(target="@everyone", deny=["view_channel"])]
        ),
    )
    assert result["channel"] == "Admin"
    targets = [target for target, _ in category.perm_calls]
    assert guild.default_role in targets and guild.me in targets  # @everyone denied, Roger kept


async def test_set_permissions_keeps_bot_access_when_hiding_from_everyone():
    guild = FakeGuild()
    channel = guild.add_text("secret")
    result = await executors.set_permissions(
        guild,
        SetPermissionsArgs(
            channel="secret", overwrites=[Overwrite(target="everyone", deny=["view_channel"])]
        ),
    )
    assert any(entry["target"] == "Roger" for entry in result["applied"])
    me_overwrite = next(ow for target, ow in channel.perm_calls if target is guild.me)
    assert me_overwrite.view_channel is True and me_overwrite.send_messages is True


async def test_set_permissions_self_keyword_resolves_to_the_bot():
    guild = FakeGuild()
    channel = guild.add_text("room")
    await executors.set_permissions(
        guild,
        SetPermissionsArgs(
            channel="room", overwrites=[Overwrite(target="self", allow=["view_channel"])]
        ),
    )
    assert any(target is guild.me for target, _ in channel.perm_calls)


async def test_preview_set_permissions_flags_kept_access_when_hiding():
    guild = FakeGuild()
    guild.add_text("secret")
    diff = await executors.preview(
        "set_permissions",
        guild,
        SetPermissionsArgs(
            channel="secret", overwrites=[Overwrite(target="@everyone", deny=["view_channel"])]
        ),
    )
    assert "Roger" in diff and "kept" in diff


async def test_create_channel_confirms_only_when_private():
    spec = schemas.REGISTRY["create_channel"]
    assert spec.needs_confirm(CreateChannelArgs(name="x", kind="text", private=True)) is True
    assert spec.needs_confirm(CreateChannelArgs(name="x", kind="text", read_only=True)) is False
    assert spec.needs_confirm(CreateChannelArgs(name="x", kind="text")) is False


async def test_preview_create_channel_shows_private_and_grants():
    guild = FakeGuild()
    diff = await executors.preview(
        "create_channel",
        guild,
        CreateChannelArgs(
            name="podcast",
            kind="text",
            category="Media",
            private=True,
            read_only=True,
            grants=[ChannelGrant(role="DJs", allow=["send_messages"])],
        ),
    )
    assert "create text channel: podcast" in diff and "under Media" in diff
    assert "private" in diff and "DJs: allow[send_messages]" in diff


# --------------------------------------------------------------------------- edit_channel / post

async def test_edit_channel_renames_and_slugs_text():
    guild = FakeGuild()
    channel = guild.add_text("general")
    result = await executors.edit_channel(
        guild, EditChannelArgs(channel="general", name="The Lobby")
    )
    assert result["name"] == "the-lobby"  # text names are slugged
    assert channel.edited == {"name": "the-lobby"}


async def test_edit_channel_moves_into_category():
    guild = FakeGuild()
    channel = guild.add_text("podcast")
    media = guild.add_category("Media")
    result = await executors.edit_channel(
        guild, EditChannelArgs(channel="podcast", category="Media")
    )
    assert result["category"] == "Media"
    assert channel.edited["category"] is media


async def test_edit_channel_rejects_topic_on_non_text():
    guild = FakeGuild()
    guild.voice_channels.append(FakeChannel(guild._id(), "Lounge"))
    with pytest.raises(GuardError):
        await executors.edit_channel(guild, EditChannelArgs(channel="Lounge", topic="nope"))


async def test_edit_channel_rejects_duplicate_rename():
    guild = FakeGuild()
    guild.add_text("general")
    guild.add_text("random")
    with pytest.raises(GuardError):
        await executors.edit_channel(guild, EditChannelArgs(channel="random", name="general"))


async def test_edit_channel_requires_at_least_one_change():
    with pytest.raises(ValueError):  # pydantic model validator
        EditChannelArgs(channel="general")


async def test_edit_channel_unknown_channel_errors():
    guild = FakeGuild()
    with pytest.raises(GuardError):
        await executors.edit_channel(guild, EditChannelArgs(channel="ghost", name="x"))


async def test_post_message_sends_with_mentions_suppressed():
    guild = FakeGuild()
    channel = guild.add_text("announcements")
    result = await executors.post_message(
        guild, PostMessageArgs(channel="announcements", content="@everyone hi")
    )
    assert result == {"posted": True, "channel": "announcements", "chars": len("@everyone hi")}
    sent = channel.sent[0]
    assert sent.content == "@everyone hi"
    # The raw text may contain @everyone, but AllowedMentions.none() means nobody is pinged.
    mentions = sent.allowed_mentions
    assert mentions.everyone is False and mentions.roles is False and mentions.users is False


async def test_post_message_rejects_non_text_channel():
    guild = FakeGuild()
    guild.voice_channels.append(FakeChannel(guild._id(), "Lounge"))
    with pytest.raises(GuardError):
        await executors.post_message(guild, PostMessageArgs(channel="Lounge", content="hi"))


async def test_preview_renders_edit_and_post():
    guild = FakeGuild()
    guild.add_text("general", topic="old topic")
    guild.add_text("announcements")
    edit_diff = await executors.preview(
        "edit_channel", guild, EditChannelArgs(channel="general", name="Chat", topic="new topic")
    )
    assert "name: general → chat" in edit_diff and "topic: old topic → new topic" in edit_diff
    post_diff = await executors.preview(
        "post_message", guild, PostMessageArgs(channel="announcements", content="ship it")
    )
    assert "post to #announcements" in post_diff and "ship it" in post_diff


# --------------------------------------------------------------------------- digest feed tools


class _FakeParsed(dict):
    """Stand-in for feedparser's FeedParserDict: dict-get and attribute access on the same keys."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


def _good_feed(title="Example", n=3, status=200):
    return _FakeParsed(
        version="rss20", status=status, feed=_FakeParsed(title=title), entries=[object()] * n
    )


@pytest.fixture
async def feeds(monkeypatch, tmp_path):
    """A patched feedparser plus a real temp store wired into a ToolContext."""
    responses: dict = {}

    def fake_parse(url):
        item = responses.get(url)
        if isinstance(item, Exception):
            raise item
        # Unknown URLs parse to something with no version — i.e. "not a recognized feed".
        return item if item is not None else _FakeParsed(version="", status=200, entries=[])

    monkeypatch.setattr(executors.feedparser, "parse", fake_parse)
    store = await Store(str(tmp_path / "feeds.db")).open()
    try:
        yield SimpleNamespace(responses=responses, store=store, ctx=ToolContext(store=store))
    finally:
        await store.close()


async def test_add_feed_validates_and_persists(feeds):
    feeds.responses["http://good"] = _good_feed(title="Good Blog", n=5)
    out = await executors.add_feed(None, AddFeedArgs(url="http://good"), feeds.ctx)
    assert out["added"] is True
    assert out["title"] == "Good Blog"
    assert out["entries"] == 5
    assert [f["url"] for f in await feeds.store.list_feeds()] == ["http://good"]


async def test_add_feed_rejects_non_feed(feeds):
    # Not in responses -> fake returns version="" -> not a recognized feed; nothing persisted.
    out = await executors.add_feed(None, AddFeedArgs(url="http://nope"), feeds.ctx)
    assert out["added"] is False
    assert "not a recognized" in out["error"]
    assert await feeds.store.count_feeds() == 0


async def test_add_feed_reports_http_error(feeds):
    feeds.responses["http://dead"] = _good_feed(status=503)
    out = await executors.add_feed(None, AddFeedArgs(url="http://dead"), feeds.ctx)
    assert out["added"] is False
    assert "503" in out["error"]


async def test_add_feed_is_idempotent(feeds):
    feeds.responses["http://good"] = _good_feed()
    await executors.add_feed(None, AddFeedArgs(url="http://good"), feeds.ctx)
    out = await executors.add_feed(None, AddFeedArgs(url="http://good"), feeds.ctx)
    assert out["added"] is False
    assert out["note"] == "already in the feed list"
    assert await feeds.store.count_feeds() == 1


async def test_remove_feed_hit_and_miss(feeds):
    feeds.responses["http://good"] = _good_feed()
    await executors.add_feed(None, AddFeedArgs(url="http://good"), feeds.ctx)
    assert (await executors.remove_feed(None, RemoveFeedArgs(url="http://good"), feeds.ctx))[
        "removed"
    ] is True
    miss = await executors.remove_feed(None, RemoveFeedArgs(url="http://good"), feeds.ctx)
    assert miss["removed"] is False


async def test_list_feeds_returns_current(feeds):
    feeds.responses["http://a"] = _good_feed(title="A")
    await executors.add_feed(None, AddFeedArgs(url="http://a"), feeds.ctx)
    out = await executors.list_feeds(None, ListFeedsArgs(), feeds.ctx)
    assert out["count"] == 1
    assert out["feeds"][0] == {"url": "http://a", "title": "A"}


async def test_suggest_feeds_validates_without_persisting(feeds):
    feeds.responses["http://ok"] = _good_feed(title="OK", n=2)
    feeds.responses["http://bad"] = RuntimeError("boom")
    out = await executors.suggest_feeds(
        None,
        SuggestFeedsArgs(urls=["http://ok", "http://bad", "http://unknown"]),
        feeds.ctx,
    )
    by_url = {c["url"]: c for c in out["candidates"]}
    assert by_url["http://ok"]["ok"] is True and by_url["http://ok"]["entries"] == 2
    assert by_url["http://bad"]["ok"] is False and "fetch failed" in by_url["http://bad"]["error"]
    assert by_url["http://unknown"]["ok"] is False  # not a recognized feed
    assert await feeds.store.count_feeds() == 0  # suggest never writes


async def test_feed_tool_without_store_raises_guard_error():
    with pytest.raises(GuardError):
        await executors.list_feeds(None, ListFeedsArgs(), None)
