"""Executor mutations against a fake guild — verifies the security invariants structurally."""

from types import SimpleNamespace

import discord
import pytest

from roger.tools import executors
from roger.tools.guard import GuardError
from roger.tools.schemas import CreateChannelArgs, CreateRoleArgs


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
