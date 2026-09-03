"""Dispatch routing — the owner gate and message classification, exercised with fakes."""

import json
import logging
from types import SimpleNamespace

import discord
import pytest

from roger import bot
from roger.bot import Route, _JsonFormatter, _missing_permissions, classify_message
from roger.request_context import current_request_id, request_context

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
    return classify_message(
        message,
        owner_id=OWNER,
        bot_user_id=BOT,
        guild_id=_IN_GUILD.id,
    )


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


def test_owner_mention_in_another_guild_is_ignored():
    assert (
        route(msg(OWNER, "roger make a channel", guild=SimpleNamespace(id=10), mentions=[BOT]))
        is Route.IGNORE
    )


def test_nonowner_mention_in_another_guild_is_ignored():
    assert (
        route(msg(OTHER, "hey roger", guild=SimpleNamespace(id=10), mentions=[BOT])) is Route.IGNORE
    )


def test_guild_message_without_mention_is_ignored():
    assert route(msg(OTHER, "just chatting", mentions=[])) is Route.IGNORE


def test_owner_gets_no_special_treatment_in_guild_without_mention():
    assert route(msg(OWNER, "talking in a channel", mentions=[])) is Route.IGNORE


def test_request_context_generates_distinct_opaque_ids_and_resets_after_exception():
    with request_context() as first:
        assert len(first) == 16 and int(first, 16) >= 0
    assert current_request_id() is None

    try:
        with request_context() as second:
            raise RuntimeError(second)
    except RuntimeError:
        pass
    assert current_request_id() is None
    assert first != second


def test_json_formatter_includes_bound_request_id_only():
    formatter = _JsonFormatter()
    record = logging.LogRecord("roger.test", logging.INFO, __file__, 1, "hello", (), None)
    assert "request_id" not in json.loads(formatter.format(record))

    with request_context() as request_id:
        assert json.loads(formatter.format(record))["request_id"] == request_id


class _JsonLogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []
        self.setFormatter(_JsonFormatter())

    def emit(self, record):
        self.messages.append(self.format(record))


async def test_ambient_message_failure_logs_bound_request_id_and_resets(monkeypatch):
    async def fail_ambient(**kwargs):
        raise RuntimeError("ambient failed")

    monkeypatch.setattr(bot, "handle_ambient", fail_ambient)
    capture = _JsonLogCapture()
    bot.log.addHandler(capture)
    client = SimpleNamespace(
        settings=SimpleNamespace(owner_id=OWNER, guild_id=_IN_GUILD.id),
        user=SimpleNamespace(id=BOT),
        llm=None,
        store=None,
        ambient_limiter=None,
    )
    message = msg(OTHER, "hello", guild=None)
    message.channel = SimpleNamespace(id=12, send=None)
    try:
        with pytest.raises(RuntimeError, match="ambient failed"):
            await bot.RogerClient.on_message(client, message)
    finally:
        bot.log.removeHandler(capture)

    assert current_request_id() is None
    payload = json.loads(capture.messages[-1])
    assert payload["request_id"]
    assert "ambient request failed" in payload["msg"]


async def test_chat_send_failure_logs_bound_request_id_and_resets(monkeypatch):
    async def reply_ambient(**kwargs):
        return "hello"

    async def defer(**kwargs):
        return None

    async def fail_send(*args, **kwargs):
        raise RuntimeError("send failed")

    monkeypatch.setattr(bot, "handle_ambient", reply_ambient)
    capture = _JsonLogCapture()
    bot.log.addHandler(capture)
    client = SimpleNamespace(llm=None, store=None, ambient_limiter=None)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=OTHER),
        channel_id=12,
        response=SimpleNamespace(defer=defer),
        followup=SimpleNamespace(send=fail_send),
    )
    try:
        with pytest.raises(RuntimeError, match="send failed"):
            await bot._handle_chat(client, interaction, "hello")
    finally:
        bot.log.removeHandler(capture)

    assert current_request_id() is None
    payload = json.loads(capture.messages[-1])
    assert payload["request_id"]
    assert "ambient request failed" in payload["msg"]


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
