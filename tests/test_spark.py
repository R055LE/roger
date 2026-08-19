"""Spark brain — item selection/parsing and the post/skip paths (fakes + real store)."""

from types import SimpleNamespace

import discord
import pytest

from roger.brains import digest as digest_module
from roger.brains.spark import SparkParseError, _parse_choice, run_spark_job
from roger.llm import BudgetExceeded, LLMConfigError
from roger.store import Store


def _entry(entry_id, title="t", link="l", summary="s", published=None):
    return SimpleNamespace(
        id=entry_id, title=title, link=link, summary=summary, published_parsed=published
    )


def _feed(entries):
    return SimpleNamespace(entries=entries)


def _choice_text(index, blurb="A short blurb.", question="What do you think?"):
    return f"ITEM: {index}\nBLURB: {blurb}\nQUESTION: {question}"


def _resp(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


def test_parse_choice_picks_the_named_index():
    index, blurb, question = _parse_choice(_choice_text(2), n=3)
    assert index == 1  # 1-based ITEM: 2 -> 0-based index 1
    assert blurb == "A short blurb."
    assert question == "What do you think?"


def test_parse_choice_accumulates_multiline_blurb():
    text = "ITEM: 1\nBLURB: Line one.\nStill the blurb.\nQUESTION: Well?"
    _, blurb, question = _parse_choice(text, n=1)
    assert blurb == "Line one. Still the blurb."
    assert question == "Well?"


@pytest.mark.parametrize(
    "text",
    [
        "BLURB: x\nQUESTION: y",  # missing ITEM
        "ITEM: 1\nQUESTION: y",  # missing BLURB
        "ITEM: 1\nBLURB: x",  # missing QUESTION
        "ITEM: not-a-number\nBLURB: x\nQUESTION: y",  # non-integer ITEM
        "ITEM: 0\nBLURB: x\nQUESTION: y",  # out of range (too low)
        "ITEM: 5\nBLURB: x\nQUESTION: y",  # out of range (too high, n=3)
        "ITEM: 1\nBLURB: \nQUESTION: y",  # empty BLURB
        "ITEM: 1\nBLURB: x\nQUESTION: ",  # empty QUESTION
    ],
)
def test_parse_choice_raises_on_malformed_input(text):
    with pytest.raises(SparkParseError):
        _parse_choice(text, n=3)


class FakeChannel:
    def __init__(self, raise_on_send=None):
        self.sent = []
        self._raise_on_send = raise_on_send

    async def send(self, embed=None, content=None):
        if self._raise_on_send is not None:
            raise self._raise_on_send
        self.sent.append(embed if embed is not None else content)


class FakeClient:
    def __init__(self, channel=None):
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel


class FakeLLM:
    def __init__(self, script):
        self._script = list(script)

    async def complete(self, brain, messages, tools=None):
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _http_error(kind, status):
    """Build a real discord HTTP error without a live aiohttp response."""
    response = SimpleNamespace(status=status, reason="test")
    return kind(response, "boom")


def _settings(channel_id=42, tz="America/Detroit"):
    return SimpleNamespace(spark_channel_id=channel_id, tz=tz)


async def _store(tmp_path, feeds=("http://f",)):
    store = await Store(str(tmp_path / "spk.db")).open()
    for url in feeds:
        await store.add_feed(url, None)
    return store


async def test_not_configured(tmp_path):
    store = await _store(tmp_path)
    try:
        out = await run_spark_job(
            client=FakeClient(),
            settings=_settings(channel_id=None),
            llm=FakeLLM([]),
            store=store,
        )
        assert "not configured" in out["status"]
    finally:
        await store.close()


async def test_no_new_items_skips(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    try:
        monkeypatch.setattr(digest_module.feedparser, "parse", lambda url: _feed([]))
        out = await run_spark_job(
            client=FakeClient(FakeChannel()), settings=_settings(), llm=FakeLLM([]), store=store
        )
        assert out["status"] == "no new items"
    finally:
        await store.close()


async def test_posts_embed_and_marks_only_the_chosen_item_seen(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    try:
        monkeypatch.setattr(
            digest_module.feedparser,
            "parse",
            lambda url: _feed([_entry("n1", title="First"), _entry("n2", title="Second")]),
        )
        channel = FakeChannel()
        out = await run_spark_job(
            client=FakeClient(channel),
            settings=_settings(),
            llm=FakeLLM([_resp(_choice_text(2, question="Thoughts?"))]),
            store=store,
        )
        assert out["status"] == "posted" and out["title"] == "Second"
        assert len(channel.sent) == 1
        embed = channel.sent[0]
        assert isinstance(embed, discord.Embed)
        assert embed.title == "Second"
        assert embed.fields[0].value == "Thoughts?"

        # The passed-over item ("n1") must still be collectible -- only "n2" was marked seen.
        remaining = await digest_module._collect_new(["http://f"], store)
        assert [e["id"] for e in remaining] == ["n1"]
    finally:
        await store.close()


async def test_unparseable_response_skips_cleanly(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    try:
        monkeypatch.setattr(digest_module.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        channel = FakeChannel()
        out = await run_spark_job(
            client=FakeClient(channel),
            settings=_settings(),
            llm=FakeLLM([_resp("not the right format at all")]),
            store=store,
        )
        assert out["status"] == "unparseable response; skipped"
        assert channel.sent == []
        remaining = await digest_module._collect_new(["http://f"], store)
        assert len(remaining) == 1  # not marked seen -- retryable
    finally:
        await store.close()


async def test_budget_skips_post_and_stays_retryable(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    try:
        monkeypatch.setattr(digest_module.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        channel = FakeChannel()
        out = await run_spark_job(
            client=FakeClient(channel),
            settings=_settings(),
            llm=FakeLLM([BudgetExceeded("spark", 100, 50)]),
            store=store,
        )
        assert "budget" in out["status"]
        assert channel.sent == []
        remaining = await digest_module._collect_new(["http://f"], store)
        assert len(remaining) == 1
    finally:
        await store.close()


async def test_llm_not_configured(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    try:
        monkeypatch.setattr(digest_module.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        out = await run_spark_job(
            client=FakeClient(FakeChannel()),
            settings=_settings(),
            llm=FakeLLM([LLMConfigError("no models configured for spark")]),
            store=store,
        )
        assert "spark brain not configured" in out["status"]
    finally:
        await store.close()


async def test_channel_not_found(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    try:
        monkeypatch.setattr(digest_module.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        out = await run_spark_job(
            client=FakeClient(channel=None),
            settings=_settings(channel_id=99),
            llm=FakeLLM([_resp(_choice_text(1))]),
            store=store,
        )
        assert "spark channel 99 not found" in out["status"]
    finally:
        await store.close()


async def test_send_failure_is_reported(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    try:
        monkeypatch.setattr(digest_module.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        channel = FakeChannel(raise_on_send=_http_error(discord.HTTPException, 500))
        out = await run_spark_job(
            client=FakeClient(channel),
            settings=_settings(),
            llm=FakeLLM([_resp(_choice_text(1))]),
            store=store,
        )
        assert out["status"] == "delivery failed; not posted"
        remaining = await digest_module._collect_new(["http://f"], store)
        assert len(remaining) == 1  # not marked seen -- retryable
    finally:
        await store.close()
