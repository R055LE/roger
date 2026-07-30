"""Executor mutations against a fake guild — verifies the security invariants structurally."""

from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from roger.store import Store
from roger.tools import executors, members, schemas
from roger.tools.context import ToolContext
from roger.tools.guard import GuardError
from roger.tools.schemas import (
    AddFeedArgs,
    ChannelGrant,
    CreateChannelArgs,
    CreateRoleArgs,
    DeleteRoleArgs,
    EditChannelArgs,
    EditRoleArgs,
    ListAuditLogArgs,
    ListFeedsArgs,
    ListInvitesArgs,
    ListRoleMembersArgs,
    ListScheduledEventsArgs,
    ListWebhooksArgs,
    MoveChannelArgs,
    Overwrite,
    PostMessageArgs,
    RemoveFeedArgs,
    SetPermissionsArgs,
    SuggestFeedsArgs,
)


def _http_error(kind, status):
    """Build a real discord HTTP error without a live aiohttp response."""
    response = SimpleNamespace(status=status, reason="test")
    return kind(response, "boom")


class FakeRole:
    def __init__(self, role_id, name, permissions_value=0, *, managed=False):
        self.id = role_id
        self.name = name
        self.permissions = SimpleNamespace(value=permissions_value)
        self.managed = managed
        self.edited = None
        self.deleted = False

    async def edit(self, **changes):
        self.edited = changes
        for key, value in changes.items():
            setattr(self, key, value)

    async def delete(self):
        self.deleted = True


class FakeChannel:
    """A text/voice/category channel that records edits and (for text) sent messages."""

    def __init__(self, cid, name, *, category=None, topic=None):
        self.id = cid
        self.name = name
        self.category = category
        self.topic = topic
        self.edited = None
        self.moved = None  # kwargs from the last move() call
        self.sent = []
        self.perm_calls = []  # (target, overwrite) from set_permissions

    async def edit(self, **changes):
        self.edited = changes
        for key, value in changes.items():
            setattr(self, key, value)

    async def move(self, **kwargs):
        self.moved = kwargs

    async def send(self, content, allowed_mentions=None):
        self.sent.append(SimpleNamespace(content=content, allowed_mentions=allowed_mentions))

    async def set_permissions(self, target, *, overwrite):
        self.perm_calls.append((target, overwrite))


class FakeAuditEntry:
    def __init__(self, action, user, target, reason, created_at):
        self.action = SimpleNamespace(name=action)
        self.user = user
        self.target = target
        self.reason = reason
        self.created_at = created_at


class FakeInvite:
    def __init__(self, code, channel, inviter, uses, max_uses, expires_at):
        self.code = code
        self.channel = channel
        self.inviter = inviter
        self.uses = uses
        self.max_uses = max_uses
        self.expires_at = expires_at


class FakeWebhook:
    def __init__(self, name, channel):
        self.name = name
        self.channel = channel


class FakeScheduledEvent:
    def __init__(self, name, start_time, channel, location, status):
        self.name = name
        self.start_time = start_time
        self.channel = channel
        self.location = location
        self.status = SimpleNamespace(name=status)


class FakeMembersHTTP:
    """Fakes the one HTTP method role_holders() calls directly — paginates like the real
    GET /guilds/{id}/members: up to _PAGE_SIZE per page, ``after`` is the last-seen user id."""

    def __init__(self, guild):
        self._guild = guild

    async def get_members(self, guild_id, limit, after):
        if self._guild.member_fetch_error is not None:
            raise self._guild.member_fetch_error
        pool = self._guild.member_payloads
        if after is not None:
            pool = [m for m in pool if int(m["user"]["id"]) > after]
        return pool[:limit]


class FakeConnectionState:
    def __init__(self, guild):
        self.http = FakeMembersHTTP(guild)


class FakeGuild:
    def __init__(self):
        self.id = 999
        self.roles = [FakeRole(0, "@everyone")]
        self.categories = []
        self.text_channels = []
        self.voice_channels = []
        self.forums = []
        self.stage_channels = []
        self._next_id = 1000
        self.last_overwrites = None
        self.me = FakeRole(1, "Roger")  # the bot's own member (guild.me); hashable overwrite key
        self.member_payloads = []  # raw REST payloads, populated via add_member()
        self.member_fetch_error = None  # set to an exception to simulate no Members intent
        self._state = FakeConnectionState(self)
        self.audit_log_entries = []
        self.audit_log_error = None
        self.invite_list = []
        self.invites_error = None
        self.webhook_list = []
        self.webhooks_error = None
        self.scheduled_event_list = []

    async def audit_logs(self, *, limit=100):
        if self.audit_log_error is not None:
            raise self.audit_log_error
        for entry in self.audit_log_entries[:limit]:
            yield entry

    async def invites(self):
        if self.invites_error is not None:
            raise self.invites_error
        return self.invite_list

    async def webhooks(self):
        if self.webhooks_error is not None:
            raise self.webhooks_error
        return self.webhook_list

    async def fetch_scheduled_events(self):
        return self.scheduled_event_list

    def add_member(self, name, *, roles=()):
        """Append a raw REST member payload — the shape GET /guilds/{id}/members actually
        returns, since role_holders() reads payloads directly rather than building a real
        discord.Member (see roger/tools/members.py)."""
        payload = {
            "user": {"id": self._id(), "username": name, "global_name": None},
            "nick": None,
            "roles": [str(r.id) for r in roles],
        }
        self.member_payloads.append(payload)
        return payload

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

    def add_role(self, name, *, managed=False):
        role = FakeRole(self._id(), name, managed=managed)
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

    def add_forum(self, name, *, category=None, topic=None):
        channel = FakeChannel(self._id(), name, category=category, topic=topic)
        self.forums.append(channel)
        return channel

    def add_stage(self, name, *, category=None):
        channel = FakeChannel(self._id(), name, category=category)
        self.stage_channels.append(channel)
        return channel

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


# ------------------------------------------------------------------ registry ↔ executors parity


def test_registry_and_executors_have_matching_tools():
    """Every schema has an executor and vice versa — a mismatch degrades a tool call to a runtime
    error (or leaves dead code) instead of failing loudly at review time."""
    assert set(schemas.REGISTRY) == set(executors.EXECUTORS)


async def test_confirm_gated_tools_have_a_real_preview():
    """Confirm-gating is the actual safety mechanism (ADR-0007) — every tool the owner must approve
    needs a genuine diff in executors.preview(), not the bare tool-name fallback for unhandled
    names. A future confirm-gated tool added without a preview branch would show the owner just its
    name as the "diff" to approve."""
    guild = FakeGuild()
    guild.add_role("DJs")
    guild.add_text("general")

    args_by_tool = {
        "create_channel": CreateChannelArgs(name="new-room", kind="text", private=True),
        "edit_role": EditRoleArgs(role="DJs", name="Selectors"),
        "delete_role": DeleteRoleArgs(role="DJs"),
        "set_permissions": SetPermissionsArgs(
            channel="general", overwrites=[Overwrite(target="@everyone", deny=["view_channel"])]
        ),
        "edit_channel": EditChannelArgs(channel="general", name="renamed"),
        "post_message": PostMessageArgs(channel="general", content="hi"),
        "move_channel": MoveChannelArgs(channel="general", position="top"),
    }
    confirm_gated = [
        name
        for name, spec in schemas.REGISTRY.items()
        if spec.requires_confirm or spec.confirm_when is not None
    ]
    assert set(confirm_gated) == set(args_by_tool)  # this test covers every confirm-gated tool

    for name in confirm_gated:
        diff = await executors.preview(name, guild, args_by_tool[name])
        assert diff != name  # not the bare fallback


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


# ------------------------------------------------------------------ edit_role / delete_role


async def test_edit_role_renames():
    guild = FakeGuild()
    guild.add_role("DJs")
    result = await executors.edit_role(guild, EditRoleArgs(role="DJs", name="Selectors"))
    assert result["name"] == "Selectors"
    assert guild.roles[-1].name == "Selectors"


async def test_edit_role_changes_color_hoist_mentionable():
    guild = FakeGuild()
    role = guild.add_role("DJs")
    result = await executors.edit_role(
        guild, EditRoleArgs(role="DJs", color="#00FF00", hoist=True, mentionable=True)
    )
    assert result["hoist"] is True
    assert result["mentionable"] is True
    assert role.edited["hoist"] is True
    assert role.edited["mentionable"] is True


async def test_edit_role_rejects_duplicate_rename():
    guild = FakeGuild()
    guild.add_role("DJs")
    guild.add_role("MCs")
    with pytest.raises(GuardError):
        await executors.edit_role(guild, EditRoleArgs(role="MCs", name="DJs"))


async def test_edit_role_rejects_everyone():
    guild = FakeGuild()
    with pytest.raises(GuardError):
        await executors.edit_role(guild, EditRoleArgs(role="@everyone", name="Nope"))


async def test_edit_role_rejects_managed():
    guild = FakeGuild()
    guild.add_role("Booster", managed=True)
    with pytest.raises(GuardError):
        await executors.edit_role(guild, EditRoleArgs(role="Booster", name="Nope"))


async def test_edit_role_requires_at_least_one_change():
    with pytest.raises(ValueError):  # pydantic model validator
        EditRoleArgs(role="DJs")


async def test_delete_role_removes_it():
    guild = FakeGuild()
    role = guild.add_role("DJs")
    result = await executors.delete_role(guild, DeleteRoleArgs(role="DJs"))
    assert result == {"deleted": "role", "name": "DJs"}
    assert role.deleted is True


async def test_delete_role_rejects_everyone():
    guild = FakeGuild()
    with pytest.raises(GuardError):
        await executors.delete_role(guild, DeleteRoleArgs(role="@everyone"))


async def test_delete_role_rejects_managed():
    guild = FakeGuild()
    guild.add_role("Booster", managed=True)
    with pytest.raises(GuardError):
        await executors.delete_role(guild, DeleteRoleArgs(role="Booster"))


async def test_preview_edit_role_shows_changes():
    guild = FakeGuild()
    guild.add_role("DJs")
    preview = await executors.preview(
        "edit_role", guild, EditRoleArgs(role="DJs", name="Selectors", hoist=True)
    )
    assert "DJs → Selectors" in preview
    assert "hoist: → True" in preview


async def test_preview_delete_role_falls_back_without_members_intent():
    guild = FakeGuild()
    guild.add_role("DJs")
    guild.member_fetch_error = _http_error(discord.Forbidden, 403)
    preview = await executors.preview("delete_role", guild, DeleteRoleArgs(role="DJs"))
    assert "PERMANENTLY DELETE" in preview
    assert "Members intent not enabled" in preview


async def test_preview_delete_role_shows_no_current_holders():
    guild = FakeGuild()
    guild.add_role("DJs")
    preview = await executors.preview("delete_role", guild, DeleteRoleArgs(role="DJs"))
    assert "held by no one" in preview


async def test_preview_delete_role_lists_current_holders():
    guild = FakeGuild()
    role = guild.add_role("DJs")
    other = guild.add_role("MCs")
    guild.add_member("Alice", roles=[role])
    guild.add_member("Bob", roles=[other])
    guild.add_member("Carol", roles=[role])
    preview = await executors.preview("delete_role", guild, DeleteRoleArgs(role="DJs"))
    assert "held by 2 member(s): Alice, Carol" in preview


async def test_role_holders_filters_by_role():
    guild = FakeGuild()
    role = guild.add_role("DJs")
    other = guild.add_role("MCs")
    guild.add_member("Alice", roles=[role])
    guild.add_member("Bob", roles=[other])
    assert await members.role_holders(guild, role) == ["Alice"]


async def test_role_holders_paginates(monkeypatch):
    """Hand-rolled since role_holders bypasses Guild.fetch_members() (see roger/tools/members.py)
    — prove the page-size/after-cursor loop actually walks more than one page."""
    monkeypatch.setattr(members, "_PAGE_SIZE", 2)
    guild = FakeGuild()
    role = guild.add_role("DJs")
    for name in ("Alice", "Bob", "Carol", "Dave", "Eve"):
        guild.add_member(name, roles=[role])
    assert await members.role_holders(guild, role) == ["Alice", "Bob", "Carol", "Dave", "Eve"]


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


def test_overwrite_rejects_allow_deny_overlap():
    with pytest.raises(ValueError):  # pydantic model validator
        Overwrite(target="@everyone", allow=["view_channel"], deny=["view_channel"])


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


async def test_edit_channel_names_the_kind_for_a_forum():
    """A forum channel is real (and visible in list_structure) but no tool supports one — the
    error should say so instead of the misleading "no match" a truly unknown name would get."""
    guild = FakeGuild()
    guild.add_forum("questions")
    with pytest.raises(GuardError, match="forum"):
        await executors.edit_channel(guild, EditChannelArgs(channel="questions", name="x"))


async def test_edit_channel_names_the_kind_for_a_stage():
    guild = FakeGuild()
    guild.add_stage("town-hall")
    with pytest.raises(GuardError, match="stage"):
        await executors.edit_channel(guild, EditChannelArgs(channel="town-hall", name="x"))


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


# --------------------------------------------------------------------------- move_channel


async def test_move_channel_to_top_uses_beginning():
    guild = FakeGuild()
    channel = guild.add_text("rules")
    result = await executors.move_channel(guild, MoveChannelArgs(channel="rules", position="top"))
    assert result == {"moved": "rules", "kind": "text", "position": "top"}
    assert channel.moved == {"beginning": True}


async def test_move_channel_to_bottom_uses_end():
    guild = FakeGuild()
    channel = guild.add_text("rules")
    await executors.move_channel(guild, MoveChannelArgs(channel="rules", position="bottom"))
    assert channel.moved == {"end": True}


async def test_move_channel_before_sibling_in_same_category():
    guild = FakeGuild()
    media = guild.add_category("Media")
    rules = guild.add_text("rules", category=media)
    general = guild.add_text("general", category=media)
    result = await executors.move_channel(
        guild, MoveChannelArgs(channel="rules", before="general")
    )
    assert result["position"] == "before general"
    assert rules.moved == {"before": general}


async def test_move_channel_after_sibling():
    guild = FakeGuild()
    rules = guild.add_text("rules")
    general = guild.add_text("general")
    await executors.move_channel(guild, MoveChannelArgs(channel="rules", after="general"))
    assert rules.moved == {"after": general}


async def test_move_channel_rejects_cross_category_reference():
    guild = FakeGuild()
    media = guild.add_category("Media")
    admin = guild.add_category("Admin")
    guild.add_text("rules", category=media)
    guild.add_text("logs", category=admin)
    with pytest.raises(GuardError):
        await executors.move_channel(guild, MoveChannelArgs(channel="rules", before="logs"))


async def test_move_category_before_another_category():
    guild = FakeGuild()
    media = guild.add_category("Media")
    admin = guild.add_category("Admin")
    result = await executors.move_channel(guild, MoveChannelArgs(channel="Admin", before="Media"))
    assert result["kind"] == "category"
    assert admin.moved == {"before": media}


async def test_move_channel_rejects_category_vs_channel_mix():
    guild = FakeGuild()
    guild.add_category("Media")
    guild.add_text("rules")
    with pytest.raises(GuardError):
        await executors.move_channel(guild, MoveChannelArgs(channel="rules", before="Media"))


async def test_move_channel_rejects_relative_to_itself():
    guild = FakeGuild()
    guild.add_text("rules")
    with pytest.raises(GuardError):
        await executors.move_channel(guild, MoveChannelArgs(channel="rules", after="rules"))


async def test_move_channel_requires_exactly_one_anchor():
    with pytest.raises(ValueError):  # pydantic model validator — none given
        MoveChannelArgs(channel="rules")
    with pytest.raises(ValueError):  # two anchors given
        MoveChannelArgs(channel="rules", position="top", before="general")


async def test_preview_move_channel_reads_naturally():
    guild = FakeGuild()
    media = guild.add_category("Media")
    guild.add_text("rules", category=media)
    guild.add_text("general", category=media)
    top_diff = await executors.preview(
        "move_channel", guild, MoveChannelArgs(channel="rules", position="top")
    )
    assert top_diff == "move text channel rules to the top of its group"
    rel_diff = await executors.preview(
        "move_channel", guild, MoveChannelArgs(channel="rules", before="general")
    )
    assert rel_diff == "move text channel rules before general"


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


# ------------------------------------------------------------------ read-only server info


async def test_list_role_members_returns_current_holders():
    guild = FakeGuild()
    role = guild.add_role("DJs")
    guild.add_member("Alice", roles=[role])
    guild.add_member("Bob", roles=[])
    out = await executors.list_role_members(guild, ListRoleMembersArgs(role="DJs"))
    assert out == {"role": "DJs", "available": True, "count": 1, "members": ["Alice"]}


async def test_list_role_members_falls_back_without_members_intent():
    guild = FakeGuild()
    guild.add_role("DJs")
    guild.member_fetch_error = _http_error(discord.Forbidden, 403)
    out = await executors.list_role_members(guild, ListRoleMembersArgs(role="DJs"))
    assert out["available"] is False


async def test_list_audit_log_reports_entries():
    guild = FakeGuild()
    when = datetime(2026, 1, 1, tzinfo=UTC)
    guild.audit_log_entries = [
        FakeAuditEntry(
            "channel_delete", SimpleNamespace(display_name="Ross"), None, "cleanup", when
        )
    ]
    out = await executors.list_audit_log(guild, ListAuditLogArgs(limit=20))
    assert out["count"] == 1
    assert out["entries"][0]["action"] == "channel_delete"
    assert out["entries"][0]["user"] == "Ross"
    assert out["entries"][0]["reason"] == "cleanup"


async def test_list_audit_log_forbidden_names_the_permission():
    guild = FakeGuild()
    guild.audit_log_error = _http_error(discord.Forbidden, 403)
    with pytest.raises(GuardError, match="View Audit Log"):
        await executors.list_audit_log(guild, ListAuditLogArgs(limit=20))


async def test_list_invites_reports_current_invites():
    guild = FakeGuild()
    channel = guild.add_text("general")
    when = datetime(2026, 6, 1, tzinfo=UTC)
    guild.invite_list = [
        FakeInvite("abc123", channel, SimpleNamespace(display_name="Ross"), 3, 10, when)
    ]
    out = await executors.list_invites(guild, ListInvitesArgs())
    assert out["count"] == 1
    assert out["invites"][0]["code"] == "abc123"
    assert out["invites"][0]["channel"] == "general"
    assert out["invites"][0]["uses"] == 3


async def test_list_invites_forbidden_names_the_permission():
    guild = FakeGuild()
    guild.invites_error = _http_error(discord.Forbidden, 403)
    with pytest.raises(GuardError, match="Manage Guild"):
        await executors.list_invites(guild, ListInvitesArgs())


async def test_list_webhooks_reports_current_webhooks():
    guild = FakeGuild()
    channel = guild.add_text("alerts")
    guild.webhook_list = [FakeWebhook("Feed Poster", channel)]
    out = await executors.list_webhooks(guild, ListWebhooksArgs())
    assert out == {"count": 1, "webhooks": [{"name": "Feed Poster", "channel": "alerts"}]}


async def test_list_webhooks_forbidden_names_the_permission():
    guild = FakeGuild()
    guild.webhooks_error = _http_error(discord.Forbidden, 403)
    with pytest.raises(GuardError, match="Manage Webhooks"):
        await executors.list_webhooks(guild, ListWebhooksArgs())


async def test_list_scheduled_events_reports_current_events():
    guild = FakeGuild()
    channel = guild.add_text("stage")
    when = datetime(2026, 8, 1, tzinfo=UTC)
    guild.scheduled_event_list = [
        FakeScheduledEvent("Movie Night", when, channel, None, "scheduled")
    ]
    out = await executors.list_scheduled_events(guild, ListScheduledEventsArgs())
    assert out["count"] == 1
    assert out["events"][0]["name"] == "Movie Night"
    assert out["events"][0]["location"] == "stage"
    assert out["events"][0]["status"] == "scheduled"
