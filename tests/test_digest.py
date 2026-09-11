"""Digest brain — Scout collection, dedupe, and the post/skip paths (fakes + real store)."""

from types import SimpleNamespace

import discord
from conftest import write_digest as _write_digest

from roger.brains import digest
from roger.brains.digest import run_digest_job
from roger.llm import BudgetExceeded
from roger.scout_source import collect_from_scout
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


def _settings(channel_id=42, tz="America/Detroit", tmp_path=None, max_age_hours=36):
    return SimpleNamespace(
        digest_channel_id=channel_id,
        tz=tz,
        scout_digest_path=(tmp_path / "digests") if tmp_path else None,
        scout_max_age_hours=max_age_hours,
    )


async def _store(tmp_path):
    return await Store(str(tmp_path / "dig.db")).open()








async def _unseen(tmp_path, store):
    """Non-destructive: how many digest items are still unseen.

    Deliberately not "re-run the job" — that would post and mark them seen,
    which is the opposite of what these assertions are checking.
    """
    batch = await collect_from_scout(
        tmp_path / "digests", store, max_age_hours=36, limit=50
    )
    return len(batch.entries)


async def test_not_configured(tmp_path):
    store = await _store(tmp_path)
    try:
        out = await run_digest_job(
            client=FakeClient(FakeChannel()),
            settings=_settings(channel_id=None, tmp_path=tmp_path),
            llm=FakeLLM([]),
            store=store,
        )
        assert out["status"] == "digest destination unset"
    finally:
        await store.close()


async def test_no_new_items_skips(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    try:
        _write_digest(tmp_path, [])
        out = await run_digest_job(
            client=FakeClient(FakeChannel()),
            settings=_settings(tmp_path=tmp_path),
            llm=FakeLLM([]),
            store=store,
        )
        assert out["status"] == "no new items"
    finally:
        await store.close()


async def test_posts_embed_and_dedupes_next_run(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    try:
        _write_digest(tmp_path, [_entry("n1"), _entry("n2")])
        channel = FakeChannel()
        out = await run_digest_job(
            client=FakeClient(channel),
            settings=_settings(tmp_path=tmp_path),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert out["status"] == "posted" and out["count"] == 2
        assert len(channel.sent) == 1
        assert isinstance(channel.sent[0], discord.Embed)

        out2 = await run_digest_job(
            client=FakeClient(channel),
            settings=_settings(tmp_path=tmp_path),
            llm=FakeLLM([]),
            store=store,
        )
        assert out2["status"] == "no new items"  # marked seen after the first post
    finally:
        await store.close()


async def test_budget_skips_post_and_stays_retryable(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    try:
        _write_digest(tmp_path, [_entry("n1")])
        channel = FakeChannel()
        out = await run_digest_job(
            client=FakeClient(channel),
            settings=_settings(tmp_path=tmp_path),
            llm=FakeLLM([BudgetExceeded("digest", 100, 50)]),
            store=store,
        )
        assert "budget" in out["status"]
        assert channel.sent == []  # nothing posted
        # Not marked seen: a second run still finds it.
        again = await run_digest_job(
            client=FakeClient(FakeChannel()),
            settings=_settings(tmp_path=tmp_path),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert again["status"] == "posted"
    finally:
        await store.close()


async def test_forbidden_send_returns_sanitized_failure_and_stays_retryable(
    tmp_path, monkeypatch, caplog
):
    store = await _store(tmp_path)
    try:
        _write_digest(tmp_path, [_entry("n1")])
        channel = FakeChannel(raise_on_send=_http_error(discord.Forbidden, 403))
        out = await run_digest_job(
            client=FakeClient(channel),
            settings=_settings(tmp_path=tmp_path),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert out["status"] == "delivery failed"
        assert any(record.exc_info for record in caplog.records)
        assert await _unseen(tmp_path, store) == 1  # not marked seen

        channel._raise_on_send = None
        retry = await run_digest_job(
            client=FakeClient(channel),
            settings=_settings(tmp_path=tmp_path),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert retry == {"status": "posted", "count": 1}
        assert await _unseen(tmp_path, store) == 0  # marked seen after the post
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


def _personal_settings(channel_id=None, tz="America/Detroit", owner_id=1, tmp_path=None,
                       max_age_hours=36):
    return SimpleNamespace(
        personal_digest_channel_id=channel_id, tz=tz, owner_id=owner_id,
        scout_digest_path=(tmp_path / "digests") if tmp_path else None,
        scout_max_age_hours=max_age_hours,
    )


async def _personal_store(tmp_path):
    return await Store(str(tmp_path / "pdig.db")).open()






async def test_personal_no_new_items_skips(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        _write_digest(tmp_path, [])
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=FakeUser()),
            settings=_personal_settings(tmp_path=tmp_path),
            llm=FakeLLM([]),
            store=store,
        )
        assert out["status"] == "no new items"
    finally:
        await store.close()


async def test_personal_posts_via_dm_when_no_channel_configured(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        _write_digest(tmp_path, [_entry("n1")])
        user = FakeUser()
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(channel_id=None, tmp_path=tmp_path),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert out["status"] == "posted" and out["count"] == 1
        assert len(user.dm_channel.sent) == 1
        assert isinstance(user.dm_channel.sent[0], discord.Embed)

        out2 = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(channel_id=None, tmp_path=tmp_path),
            llm=FakeLLM([]),
            store=store,
        )
        assert out2["status"] == "no new items"  # marked seen after the first post
    finally:
        await store.close()


async def test_personal_posts_to_channel_when_configured(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        _write_digest(tmp_path, [_entry("n1")])
        channel = FakeChannel()
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=FakeUser(), channel=channel),
            settings=_personal_settings(channel_id=99, tmp_path=tmp_path),
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
        _write_digest(tmp_path, [_entry("n1")])
        user = FakeUser(raise_on_create_dm=_http_error(discord.Forbidden, 403))
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(channel_id=None, tmp_path=tmp_path),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert "DM failed" in out["status"]
    finally:
        await store.close()


async def test_personal_budget_skips_post_and_stays_retryable(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        _write_digest(tmp_path, [_entry("n1")])
        user = FakeUser()
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(channel_id=None, tmp_path=tmp_path),
            llm=FakeLLM([BudgetExceeded("digest", 100, 50)]),
            store=store,
        )
        assert "budget" in out["status"]
        assert user.dm_channel.sent == []
        assert await _unseen(tmp_path, store) == 1  # not marked seen
    finally:
        await store.close()


async def test_personal_send_failure_is_reported(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        _write_digest(tmp_path, [_entry("n1")])
        user = FakeUser(raise_on_send=_http_error(discord.HTTPException, 500))
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(channel_id=None, tmp_path=tmp_path),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert out["status"] == "delivery failed; digest not sent"
        # Item not marked seen after send failure, so it's retryable
        assert await _unseen(tmp_path, store) == 1
    finally:
        await store.close()
