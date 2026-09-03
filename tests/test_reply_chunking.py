"""Splitting long replies into multiple Discord messages instead of truncating them."""

import discord

import roger.bot as bot
from roger.bot import _chunk, _send_chunked, _send_report


def test_chunk_returns_single_chunk_when_under_limit():
    assert _chunk("short reply", limit=20) == ["short reply"]


def test_chunk_splits_on_the_last_newline_within_the_limit():
    text = "a" * 10 + "\n" + "b" * 10
    chunks = _chunk(text, limit=15)
    assert chunks == ["a" * 10, "b" * 10]


def test_chunk_hard_cuts_when_no_newline_is_available():
    text = "x" * 45
    chunks = _chunk(text, limit=20)
    assert chunks == ["x" * 20, "x" * 20, "x" * 5]


def test_chunk_never_exceeds_the_limit_and_drops_no_non_newline_content():
    paragraphs = ["Paragraph one is here.", "Paragraph two follows.", "y" * 50, "Short tail."]
    text = "\n\n".join(paragraphs)
    chunks = _chunk(text, limit=25)
    assert all(0 < len(c) <= 25 for c in chunks)
    # Splitting drops the newline(s) at the split point (no leading blank lines in a continuation
    # message) but must never drop any actual content.
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


async def test_send_chunked_sends_one_message_when_short():
    sent = []

    async def fake_send(content, **kwargs):
        sent.append((content, kwargs))

    await _send_chunked(fake_send, "hello")
    assert sent[0][0] == "hello"
    allowed_mentions = sent[0][1]["allowed_mentions"]
    assert allowed_mentions.everyone is False
    assert allowed_mentions.roles is False
    assert allowed_mentions.users is False


async def test_send_chunked_sends_multiple_messages_in_order_when_long():
    sent = []

    async def fake_send(content, **kwargs):
        sent.append((content, kwargs))

    text = "x" * 4500
    await _send_chunked(fake_send, text)
    assert len(sent) == 3
    assert all(len(chunk) <= 2000 for chunk, _ in sent)
    assert "".join(chunk for chunk, _ in sent) == text
    assert all(kwargs["allowed_mentions"].everyone is False for _, kwargs in sent)


async def test_send_report_sends_plain_text_when_short():
    calls = []

    async def fake_send(content, **kwargs):
        calls.append((content, kwargs))

    await _send_report(fake_send, "short analysis")
    assert calls[0][0] == "short analysis"
    allowed_mentions = calls[0][1]["allowed_mentions"]
    assert allowed_mentions.everyone is False
    assert allowed_mentions.roles is False
    assert allowed_mentions.users is False


async def test_send_report_attaches_a_file_when_long():
    calls = []

    async def fake_send(content, **kwargs):
        calls.append((content, kwargs))

    text = "y" * 2500
    await _send_report(fake_send, text)
    assert len(calls) == 1
    content, kwargs = calls[0]
    assert "attached" in content.lower()
    file = kwargs["file"]
    assert isinstance(file, discord.File)
    assert file.filename == "gigabrain-report.md"
    assert file.fp.read().decode("utf-8") == text
    allowed_mentions = kwargs["allowed_mentions"]
    assert allowed_mentions.everyone is False
    assert allowed_mentions.roles is False
    assert allowed_mentions.users is False


async def test_confirmation_preview_suppresses_mentions(monkeypatch):
    class FakeView:
        def __init__(self, owner_id):
            self.owner_id = owner_id
            self.value = False

        async def wait(self):
            return None

    calls = []

    async def fake_send(*, content, **kwargs):
        calls.append((content, kwargs))

    monkeypatch.setattr(bot, "_ConfirmView", FakeView)

    confirmer = bot._make_confirmer(fake_send, owner_id=222)
    assert await confirmer("@everyone review this") is False

    assert "@everyone review this" in calls[0][0]
    allowed_mentions = calls[0][1]["allowed_mentions"]
    assert allowed_mentions.everyone is False
    assert allowed_mentions.roles is False
    assert allowed_mentions.users is False
