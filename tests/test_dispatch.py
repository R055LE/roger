"""Dispatch routing — the owner gate and message classification, exercised with fakes."""

from types import SimpleNamespace

from roger.bot import Route, classify_message

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


def test_guild_mention_goes_to_ambient():
    assert route(msg(OTHER, "hey roger", mentions=[BOT])) is Route.AMBIENT_MENTION


def test_guild_message_without_mention_is_ignored():
    assert route(msg(OTHER, "just chatting", mentions=[])) is Route.IGNORE


def test_owner_gets_no_special_treatment_in_guild_without_mention():
    assert route(msg(OWNER, "talking in a channel", mentions=[])) is Route.IGNORE
