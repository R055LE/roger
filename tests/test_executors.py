"""Executor mutations against a fake guild — verifies the security invariants structurally."""

from types import SimpleNamespace

import discord
import pytest

from roger.store import Store
from roger.tools import executors
from roger.tools.context import ToolContext
from roger.tools.guard import GuardError
from roger.tools.schemas import (
    AddFeedArgs,
    CreateChannelArgs,
    CreateRoleArgs,
    ListFeedsArgs,
    RemoveFeedArgs,
    SuggestFeedsArgs,
)


class FakeRole:
    def __init__(self, role_id, name, permissions_value=0):
        self.id = role_id
        self.name = name
        self.permissions = SimpleNamespace(value=permissions_value)


class FakeGuild:
    def __init__(self):
        self.roles = [FakeRole(0, "@everyone")]
        self.categories = []
        self.text_channels = []
        self.voice_channels = []
        self._next_id = 1000
        self.last_overwrites = None

    @property
    def default_role(self):
        return self.roles[0]

    def _id(self):
        self._next_id += 1
        return self._next_id

    async def create_role(self, *, name, permissions, hoist, mentionable, color=None):
        role = FakeRole(self._id(), name, permissions.value)
        self.roles.append(role)
        return role

    async def create_text_channel(self, *, name, category, topic, overwrites):
        self.last_overwrites = overwrites
        channel = SimpleNamespace(id=self._id(), name=name)
        self.text_channels.append(channel)
        return channel

    async def create_category(self, *, name):
        category = SimpleNamespace(id=self._id(), name=name)
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
