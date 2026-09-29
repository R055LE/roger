"""Daily curated posting: quiet days, durable claims, and uncertain delivery."""

import json
from types import SimpleNamespace

from conftest import write_digest

from roger.brains.curated import preview_curated_job, run_curated_job
from roger.scout_source import collect_from_scout
from roger.store import Store

QUOTE_A = "The release cuts cold start latency by 30 percent in the published test."
QUOTE_B = "The maintainers also published the benchmark setup and raw measurements."
SOURCE = f"{QUOTE_A} {QUOTE_B} " * 8


def _entry():
    return SimpleNamespace(
        id="story", title="@everyone A measured release", link="https://example.org/release",
        summary="Benchmarks and release notes.", published_parsed=None,
    )


def _digest(tmp_path, *, article=True):
    run_id = write_digest(tmp_path, [_entry()])
    path = tmp_path / "digests" / f"{run_id}.json"
    payload = json.loads(path.read_text())
    if article:
        payload["items"][0]["article"] = {
            "status": "ok", "url": "https://example.org/release", "text": SOURCE,
        }
    path.write_text(json.dumps(payload))


def _response():
    content = json.dumps({
        "decision": "post", "item": 1,
        "facts": [
            {"text": "Cold starts improved by 30 percent.", "evidence": QUOTE_A},
            {"text": "Raw measurements were published.", "evidence": QUOTE_B},
        ],
        "why": "This helps compare inference servers.",
        "take": "@everyone, the raw data is the useful part.",
        "question": "",
    })
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class LLM:
    calls = 0

    def __init__(self, responses=None):
        self.responses = list(responses or [])

    async def complete(self, brain, messages):
        self.calls += 1
        assert brain == "curated"
        if self.responses:
            content = self.responses.pop(0)
            message = SimpleNamespace(content=content)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        return _response()


class Channel:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    async def send(self, *, embed, allowed_mentions):
        self.sent.append((embed, allowed_mentions))
        if self.error:
            raise self.error
        return SimpleNamespace(id=123)


def _settings(tmp_path):
    return SimpleNamespace(curated_channel_id=42, tz="UTC",
                           scout_digest_path=tmp_path / "digests", scout_max_age_hours=36)


async def test_posts_once_across_restart_and_suppresses_the_item(tmp_path):
    _digest(tmp_path)
    llm, channel = LLM(), Channel()
    client = SimpleNamespace(get_channel=lambda _: channel)
    settings = _settings(tmp_path)
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        result = await run_curated_job(client=client, settings=settings, llm=llm, store=store)
        assert result["status"] == "posted"
        assert len(channel.sent) == 1
        embed, mentions = channel.sent[0]
        assert embed.url == "https://example.org/release"
        assert "@everyone" not in embed.title + embed.description
        assert mentions.everyone is False
        assert (await collect_from_scout(
            settings.scout_digest_path, store, max_age_hours=36, limit=25
        )).entries == []
    finally:
        await store.close()
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        second = await run_curated_job(client=client, settings=settings, llm=llm, store=store)
        assert second["status"] == "already posted"
        assert llm.calls == 1
        assert len(channel.sent) == 1
    finally:
        await store.close()


async def test_uncertain_send_keeps_a_durable_claim_and_never_retries(tmp_path):
    _digest(tmp_path)
    llm, channel = LLM(), Channel(TimeoutError("reply lost"))
    client = SimpleNamespace(get_channel=lambda _: channel)
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        first = await run_curated_job(
            client=client, settings=_settings(tmp_path), llm=llm, store=store
        )
    finally:
        await store.close()
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        second = await run_curated_job(
            client=client, settings=_settings(tmp_path), llm=llm, store=store
        )
        assert first["status"] == second["status"] == "delivery uncertain; manual check required"
        assert llm.calls == 1
        assert len(channel.sent) == 1
    finally:
        await store.close()


async def test_unreadable_source_is_a_quiet_day_without_model_spend(tmp_path):
    _digest(tmp_path, article=False)
    llm, channel = LLM(), Channel()
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        result = await run_curated_job(
            client=SimpleNamespace(get_channel=lambda _: channel),
            settings=_settings(tmp_path), llm=llm, store=store,
        )
        assert result["status"] == "no post-worthy items"
        assert llm.calls == 0
        assert channel.sent == []
    finally:
        await store.close()


async def test_preview_keeps_seen_state_and_shows_supporting_quotes(tmp_path):
    _digest(tmp_path)
    llm = LLM()
    store = await Store(str(tmp_path / "roger.db")).open()
    settings = _settings(tmp_path)
    try:
        result = await preview_curated_job(settings=settings, llm=llm, store=store)
        assert result["status"] == "draft"
        assert result["source_url"] == "https://example.org/release"
        assert result["evidence"] == [QUOTE_A, QUOTE_B]
        assert len((await collect_from_scout(
            settings.scout_digest_path, store, max_age_hours=36, limit=25
        )).entries) == 1

        await store.mark_seen([("scout:f", "story")])
        again = await preview_curated_job(settings=settings, llm=llm, store=store)
        assert again["status"] == "draft", "preview should inspect even previously seen items"
    finally:
        await store.close()


async def test_preview_repairs_why_without_consuming_seen_state(tmp_path):
    _digest(tmp_path)
    original = json.loads(_response().choices[0].message.content)
    original["why"] = "The published result helps compare inference servers. " * 10
    llm = LLM([json.dumps(original), json.dumps({"sentences": [1]})])
    store = await Store(str(tmp_path / "roger.db")).open()
    settings = _settings(tmp_path)
    try:
        result = await preview_curated_job(settings=settings, llm=llm, store=store)
        assert result["status"] == "draft"
        assert result["why"] == "The published result helps compare inference servers."
        assert llm.calls == 2
        assert len((await collect_from_scout(
            settings.scout_digest_path, store, max_age_hours=36, limit=25
        )).entries) == 1
    finally:
        await store.close()
