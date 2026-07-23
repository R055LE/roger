"""Dispatch routing — the owner gate and message classification, exercised with fakes."""

from types import SimpleNamespace

import discord

from roger.bot import Route, _missing_permissions, classify_message

BOT = 111
OWNER = 222
OTHER = 333

_IN_GUILD = SimpleNamespace(id=9)


def msg(author_id, content, *, guild=_IN_GUILD, mentions=()):
    return SimpleNamespace(
        author=SimpleNamespace(id=author_id),
        content=content,
        guild=guild,
        mentions=[SimpleNamespace(id=m) for m in mentions],
    )


def route(message):
    return classify_message(message, owner_id=OWNER, bot_user_id=BOT)


def test_ignores_own_messages():
    assert route(msg(BOT, "hello there")) is Route.IGNORE


def test_ignores_empty_content():
    # e.g. a reply to Roger without a ping: no message_content intent -> blank body.
    assert route(msg(OTHER, "   ", guild=None)) is Route.IGNORE


def test_owner_dm_goes_to_admin():
    assert route(msg(OWNER, "make a channel", guild=None)) is Route.ADMIN_DM


def test_nonowner_dm_goes_to_ambient():
    assert route(msg(OTHER, "hi roger", guild=None)) is Route.AMBIENT_DM


def test_nonowner_guild_mention_goes_to_ambient():
    assert route(msg(OTHER, "hey roger", mentions=[BOT])) is Route.AMBIENT_MENTION


def test_owner_guild_mention_goes_to_admin():
    assert route(msg(OWNER, "roger make a channel", mentions=[BOT])) is Route.ADMIN_MENTION


def test_guild_message_without_mention_is_ignored():
    assert route(msg(OTHER, "just chatting", mentions=[])) is Route.IGNORE


def test_owner_gets_no_special_treatment_in_guild_without_mention():
    assert route(msg(OWNER, "talking in a channel", mentions=[])) is Route.IGNORE


# --------------------------------------------------------------------------- permission check

# The exact integer deploy/README.md tells the operator to invite with.
DOCUMENTED_INVITE = 268454928


def test_documented_invite_integer_grants_every_required_permission():
    # If this fails, the number in deploy/README.md and the code's requirements have drifted apart.
    assert _missing_permissions(discord.Permissions(DOCUMENTED_INVITE)) == []


def test_no_permissions_flags_all_required_scopes():
    assert set(_missing_permissions(discord.Permissions.none())) == {
        "View Channels",
        "Manage Channels",
        "Manage Roles",
        "Send Messages",
        "Embed Links",
    }


def test_administrator_satisfies_the_check():
    # A member with Administrator resolves to guild_permissions == Permissions.all() at runtime,
    # so nothing is reported missing (we simply never invite Administrator).
    assert _missing_permissions(discord.Permissions.all()) == []


def test_a_single_revoked_scope_is_named():
    perms = discord.Permissions(DOCUMENTED_INVITE)
    perms.update(manage_roles=False)
    assert _missing_permissions(perms) == ["Manage Roles"]
