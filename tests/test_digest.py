"""Digest brain — feed collection, dedupe, and the post/skip paths (fakes + real store)."""

import time
from types import SimpleNamespace

import discord

from roger.brains import digest
from roger.brains.digest import _collect_new, run_digest_job
from roger.llm import BudgetExceeded
from roger.store import Store


def _entry(entry_id, title="t", link="l", summary="s", published=None):
    return SimpleNamespace(
        id=entry_id, title=title, link=link, summary=summary, published_parsed=published
    )


def _feed(entries):
    return SimpleNamespace(entries=entries)


def _resp(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


class FakeChannel:
    def __init__(self, raise_on_send=None):
        self.sent = []
        self._raise_on_send = raise_on_send

    async def send(self, embed=None, content=None):
        if self._raise_on_send is not None:
            raise self._raise_on_send
        self.sent.append(embed if embed is not None else content)


class FakeClient:
    def __init__(self, channel):
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


def _settings(channel_id=42, tz="America/Detroit"):
    # feeds now live in the store, not settings — run_digest_job reads store.list_feeds().
    return SimpleNamespace(digest_channel_id=channel_id, tz=tz)


async def _store(tmp_path, feeds=("http://f",)):
    store = await Store(str(tmp_path / "dig.db")).open()
    for url in feeds:
        await store.add_feed(url, None)
    return store


async def test_collect_new_filters_seen_and_caps(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    try:
        entries = [_entry(f"e{i}", published=time.gmtime(i)) for i in range(20)]
        monkeypatch.setattr(digest.feedparser, "parse", lambda url: _feed(entries))
        await store.mark_seen([("http://f", f"e{i}") for i in range(5)])
        got = await _collect_new(["http://f"], store)
        ids = {g["id"] for g in got}
        assert "e0" not in ids and "e5" in ids
        assert len(got) == 15  # capped at MAX_ITEMS
    finally:
        await store.close()


async def test_collect_new_survives_a_dead_feed(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    try:
        def parse(url):
            if url == "bad":
                raise RuntimeError("dead feed")
            return _feed([_entry("ok1")])

        monkeypatch.setattr(digest.feedparser, "parse", parse)
        got = await _collect_new(["bad", "good"], store)
        assert [g["id"] for g in got] == ["ok1"]
    finally:
        await store.close()


async def test_seed_feeds_if_empty_is_one_shot(tmp_path):
    store = await _store(tmp_path, feeds=())  # start empty
    try:
        seeded = await digest.seed_feeds_if_empty(store, _settings_feeds(["http://s1", "http://s2"]))
        assert seeded == 2
        assert await store.count_feeds() == 2
        # A later env change does NOT re-seed once the store is populated.
        assert await digest.seed_feeds_if_empty(store, _settings_feeds(["http://s3"])) == 0
        assert {f["url"] for f in await store.list_feeds()} == {"http://s1", "http://s2"}
    finally:
        await store.close()


def _settings_feeds(feeds):
    return SimpleNamespace(feeds=list(feeds))


async def test_not_configured(tmp_path):
    store = await _store(tmp_path)
    try:
        out = await run_digest_job(
            client=FakeClient(FakeChannel()),
            settings=_settings(channel_id=None),
            llm=FakeLLM([]),
            store=store,
        )
        assert out["status"] == "digest destination unset"
    finally:
        await store.close()


async def test_no_new_items_skips(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    try:
        monkeypatch.setattr(digest.feedparser, "parse", lambda url: _feed([]))
        out = await run_digest_job(
            client=FakeClient(FakeChannel()), settings=_settings(), llm=FakeLLM([]), store=store
        )
        assert out["status"] == "no new items"
    finally:
        await store.close()


async def test_posts_embed_and_dedupes_next_run(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    try:
        monkeypatch.setattr(
            digest.feedparser, "parse", lambda url: _feed([_entry("n1"), _entry("n2")])
        )
        channel = FakeChannel()
        out = await run_digest_job(
            client=FakeClient(channel),
            settings=_settings(),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert out["status"] == "posted" and out["count"] == 2
        assert len(channel.sent) == 1
        assert isinstance(channel.sent[0], discord.Embed)

        out2 = await run_digest_job(
            client=FakeClient(channel), settings=_settings(), llm=FakeLLM([]), store=store
        )
        assert out2["status"] == "no new items"  # marked seen after the first post
    finally:
        await store.close()


async def test_budget_skips_post_and_stays_retryable(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    try:
        monkeypatch.setattr(digest.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        channel = FakeChannel()
        out = await run_digest_job(
            client=FakeClient(channel),
            settings=_settings(),
            llm=FakeLLM([BudgetExceeded("digest", 100, 50)]),
            store=store,
        )
        assert "budget" in out["status"]
        assert channel.sent == []  # nothing posted
        assert len(await _collect_new(["http://f"], store)) == 1  # not marked seen
    finally:
        await store.close()


async def test_forbidden_send_returns_sanitized_failure_and_stays_retryable(
    tmp_path, monkeypatch, caplog
):
    store = await _store(tmp_path)
    try:
        monkeypatch.setattr(digest.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        channel = FakeChannel(raise_on_send=_http_error(discord.Forbidden, 403))
        out = await run_digest_job(
            client=FakeClient(channel),
            settings=_settings(),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert out["status"] == "delivery failed"
        assert any(record.exc_info for record in caplog.records)
        assert len(await _collect_new(["http://f"], store)) == 1

        channel._raise_on_send = None
        retry = await run_digest_job(
            client=FakeClient(channel),
            settings=_settings(),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert retry == {"status": "posted", "count": 1}
        assert len(await _collect_new(["http://f"], store)) == 0
    finally:
        await store.close()


# --------------------------------------------------------------------------- personal digest


class FakeDMChannel:
    def __init__(self, raise_on_send=None):
        self.sent = []
        self._raise_on_send = raise_on_send

    async def send(self, embed=None, content=None):
        if self._raise_on_send is not None:
            raise self._raise_on_send
        self.sent.append(embed if embed is not None else content)


class FakeUser:
    def __init__(self, raise_on_create_dm=None, raise_on_send=None):
        self._raise_on_create_dm = raise_on_create_dm
        self.dm_channel = FakeDMChannel(raise_on_send=raise_on_send)

    async def create_dm(self):
        if self._raise_on_create_dm is not None:
            raise self._raise_on_create_dm
        return self.dm_channel


class FakePersonalClient:
    def __init__(self, user=None, channel=None):
        self._user = user
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel

    async def fetch_user(self, user_id):
        return self._user


def _http_error(kind, status):
    """Build a real discord HTTP error without a live aiohttp response."""
    response = SimpleNamespace(status=status, reason="test")
    return kind(response, "boom")


def _personal_settings(channel_id=None, tz="America/Detroit", owner_id=1):
    return SimpleNamespace(personal_digest_channel_id=channel_id, tz=tz, owner_id=owner_id)


async def _personal_store(tmp_path, feeds=("http://pf",)):
    store = await Store(str(tmp_path / "pdig.db")).open()
    for url in feeds:
        await store.add_personal_feed(url, None)
    return store


async def test_personal_seed_if_empty_is_one_shot(tmp_path):
    store = await _personal_store(tmp_path, feeds=())  # start empty
    try:
        seeded = await digest.seed_personal_feeds_if_empty(
            store, SimpleNamespace(personal_feeds=["http://s1", "http://s2"])
        )
        assert seeded == 2
        assert await store.count_personal_feeds() == 2
        # A later env change does NOT re-seed once the store is populated.
        again = await digest.seed_personal_feeds_if_empty(
            store, SimpleNamespace(personal_feeds=["http://s3"])
        )
        assert again == 0
    finally:
        await store.close()


async def test_personal_not_configured_when_no_feeds(tmp_path):
    store = await _personal_store(tmp_path, feeds=())
    try:
        user = FakeUser()
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(),
            llm=FakeLLM([]),
            store=store,
        )
        assert "not configured" in out["status"]
        assert user.dm_channel.sent == []
    finally:
        await store.close()


async def test_personal_no_new_items_skips(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        monkeypatch.setattr(digest.feedparser, "parse", lambda url: _feed([]))
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=FakeUser()),
            settings=_personal_settings(),
            llm=FakeLLM([]),
            store=store,
        )
        assert out["status"] == "no new items"
    finally:
        await store.close()


async def test_personal_posts_via_dm_when_no_channel_configured(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        monkeypatch.setattr(digest.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        user = FakeUser()
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(channel_id=None),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert out["status"] == "posted" and out["count"] == 1
        assert len(user.dm_channel.sent) == 1
        assert isinstance(user.dm_channel.sent[0], discord.Embed)

        out2 = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(channel_id=None),
            llm=FakeLLM([]),
            store=store,
        )
        assert out2["status"] == "no new items"  # marked seen after the first post
    finally:
        await store.close()


async def test_personal_posts_to_channel_when_configured(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        monkeypatch.setattr(digest.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        channel = FakeChannel()
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=FakeUser(), channel=channel),
            settings=_personal_settings(channel_id=99),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert out["status"] == "posted"
        assert len(channel.sent) == 1
    finally:
        await store.close()


async def test_personal_dm_creation_failure_is_reported(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        monkeypatch.setattr(digest.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        user = FakeUser(raise_on_create_dm=_http_error(discord.Forbidden, 403))
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(channel_id=None),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert "DM failed" in out["status"]
    finally:
        await store.close()


async def test_personal_budget_skips_post_and_stays_retryable(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        monkeypatch.setattr(digest.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        user = FakeUser()
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(channel_id=None),
            llm=FakeLLM([BudgetExceeded("digest", 100, 50)]),
            store=store,
        )
        assert "budget" in out["status"]
        assert user.dm_channel.sent == []
        assert len(await _collect_new(["http://pf"], store)) == 1  # not marked seen
    finally:
        await store.close()


async def test_personal_send_failure_is_reported(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        monkeypatch.setattr(digest.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        user = FakeUser(raise_on_send=_http_error(discord.HTTPException, 500))
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(channel_id=None),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert out["status"] == "delivery failed; digest not sent"
        # Item not marked seen after send failure, so it's retryable
        assert len(await _collect_new(["http://pf"], store)) == 1
    finally:
        await store.close()
