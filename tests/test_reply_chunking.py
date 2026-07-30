"""Splitting long replies into multiple Discord messages instead of truncating them."""

from roger.bot import _chunk, _send_chunked


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

    async def fake_send(content):
        sent.append(content)

    await _send_chunked(fake_send, "hello")
    assert sent == ["hello"]


async def test_send_chunked_sends_multiple_messages_in_order_when_long():
    sent = []

    async def fake_send(content):
        sent.append(content)

    text = "x" * 4500
    await _send_chunked(fake_send, text)
    assert len(sent) == 3
    assert all(len(chunk) <= 2000 for chunk in sent)
    assert "".join(sent) == text
