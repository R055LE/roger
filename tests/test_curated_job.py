"""Daily curated posting: quiet days, durable claims, and uncertain delivery."""

import asyncio
import datetime
import json
from types import SimpleNamespace

import pytest
from conftest import write_digest
from openai import APIConnectionError

from roger import store as store_module
from roger.brains import curated
from roger.brains.curated import SUPPORT_REVIEW, preview_curated_job, run_curated_job
from roger.llm import BudgetExceeded, LLMConfigError
from roger.scout_source import collect_from_scout
from roger.store import Store

QUOTE_A = "The release cuts cold start latency by 30 percent in the published test."
QUOTE_B = "The maintainers also published the benchmark setup and raw measurements."
SOURCE = f"{QUOTE_A} {QUOTE_B} " * 8


def _entry(entry_id="story"):
    return SimpleNamespace(
        id=entry_id, title="@everyone A measured release",
        link="https://example.org/release" if entry_id == "story"
        else f"https://example.org/{entry_id}",
        summary="Benchmarks and release notes.", published_parsed=None,
    )


def _digest(tmp_path, *, article=True, entries=None, now=None, run_id=None):
    run_id = write_digest(tmp_path, entries if entries is not None else [_entry()], run_id=run_id)
    path = tmp_path / "digests" / f"{run_id}.json"
    payload = json.loads(path.read_text())
    if now:
        payload["run"]["started_at"] = now.isoformat()
        for item in payload["items"]:
            item["published"] = now.isoformat()
    if article:
        for item in payload["items"]:
            item["article"] = {"status": "ok", "url": item["url"], "text": SOURCE}
    path.write_text(json.dumps(payload))
    return path


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

    async def complete(self, brain, messages, *, curated_review=False):
        self.calls += 1
        assert brain == "curated"
        assert curated_review is (messages[0]["content"] == SUPPORT_REVIEW)
        if self.responses:
            content = self.responses.pop(0)
            if isinstance(content, Exception):
                raise content
            message = SimpleNamespace(content=content)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        if curated_review:
            content = json.dumps({"supported": [True, True]})
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


def _settings(tmp_path, **overrides):
    values = dict(curated_channel_id=42, tz="UTC", curated_max_posts_per_day=1,
                  curated_min_spacing_minutes=60, curated_check_interval_minutes=0,
                  curated_max_observations_per_day=8,
                  curated_models=["test/draft"], curated_review_models=["test/review"],
                  scout_digest_path=tmp_path / "digests", scout_max_age_hours=36)
    return SimpleNamespace(**(values | overrides))


@pytest.mark.parametrize("recover", [False, True])
async def test_posts_once_across_restart_and_suppresses_the_item(tmp_path, recover):
    _digest(tmp_path)
    original = _response().choices[0].message.content
    llm = LLM([original, json.dumps({"supported": [False, True]}), original] if recover else [])
    channel = Channel()
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
        assert llm.calls == (4 if recover else 2)
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
        assert llm.calls == 2
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
        assert llm.calls == 3
        assert len((await collect_from_scout(
            settings.scout_digest_path, store, max_age_hours=36, limit=25
        )).entries) == 1
    finally:
        await store.close()


@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize("revision_responses, status", [
    ([_response().choices[0].message.content, json.dumps({"supported": [False, True]})],
     "unusable model response: fact evidence does not support every claim after revision; skipped"),
    (["not JSON"], "unusable model response: response is not JSON; skipped"),
    ([_response().choices[0].message.content, '{"supported":[true]}'],
     "unusable model response: invalid fact support review; skipped"),
    ([BudgetExceeded("curated", 30_000, 30_000)], "budget exceeded; skipped"),
    ([_response().choices[0].message.content, BudgetExceeded("curated", 30_000, 30_000)],
     "budget exceeded; skipped"),
    ([LLMConfigError("no configured model")], "curated brain not configured"),
    ([APIConnectionError(request=None)], "model request failed; skipped"),
    (['{"decision":"skip"}'], "no post-worthy items"),
])
async def test_support_recovery_failures_never_claim_send_or_consume_items(
    tmp_path, preview, revision_responses, status,
):
    _digest(tmp_path)
    original = _response().choices[0].message.content
    llm = LLM([original, json.dumps({"supported": [False, True]}), *revision_responses])
    channel = Channel()
    store = await Store(str(tmp_path / "roger.db")).open()
    settings = _settings(tmp_path)
    try:
        if preview:
            result = await preview_curated_job(settings=settings, llm=llm, store=store)
        else:
            result = await run_curated_job(
                client=SimpleNamespace(get_channel=lambda _: channel),
                settings=settings, llm=llm, store=store,
                now=datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC),
            )
        assert result["status"] == status
        assert llm.calls == 2 + len(revision_responses)
        assert channel.sent == []
        assert await store.curated_delivery("2026-10-01") is None
        assert len((await collect_from_scout(
            settings.scout_digest_path, store, max_age_hours=36, limit=25
        )).entries) == 1
    finally:
        await store.close()


async def test_stream_posts_distinct_items_with_spacing_and_a_daily_ceiling(tmp_path):
    now = datetime.datetime(2026, 10, 1, 9, tzinfo=datetime.UTC)
    _digest(tmp_path, entries=[_entry(str(i)) for i in range(3)], now=now)
    settings = _settings(tmp_path, curated_max_posts_per_day=2)
    llm, channel = LLM(), Channel()
    store = await Store(str(tmp_path / "roger.db")).open()
    kwargs = dict(client=SimpleNamespace(get_channel=lambda _: channel),
                  settings=settings, llm=llm, store=store)
    try:
        first = await run_curated_job(**kwargs, now=now)
        early = await run_curated_job(**kwargs, now=now + datetime.timedelta(minutes=30))
        second = await run_curated_job(**kwargs, now=now + datetime.timedelta(minutes=61))
        full = await run_curated_job(**kwargs, now=now + datetime.timedelta(hours=3))
        assert first["status"] == second["status"] == "posted"
        assert early["status"] == "spacing deferred"
        assert full["status"] == "daily posting limit reached"
        assert full["remaining_posts"] == 0
        assert llm.calls == 4
        assert {embed.url for embed, _ in channel.sent} == {
            "https://example.org/0", "https://example.org/1",
        }
    finally:
        await store.close()


async def test_rejected_input_survives_restart_and_new_evidence_allows_reconsideration(tmp_path):
    path = _digest(tmp_path)
    llm, channel = LLM(["not JSON"]), Channel()
    settings = _settings(tmp_path)
    store = await Store(str(tmp_path / "roger.db")).open()
    kwargs = dict(client=SimpleNamespace(get_channel=lambda _: channel), settings=settings, llm=llm)
    try:
        first = await run_curated_job(**kwargs, store=store)
        assert first["status"].startswith("unusable model response")
    finally:
        await store.close()
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        second = await run_curated_job(**kwargs, store=store)
        assert second["status"] == "unchanged input; skipped"
        assert llm.calls == 1
        assert channel.sent == []
        payload = json.loads(path.read_text())
        payload["items"][0]["article"]["text"] += " New evidence is now available."
        path.write_text(json.dumps(payload))
        assert (await run_curated_job(**kwargs, store=store))["status"] == "posted"
        assert llm.calls == 3
    finally:
        await store.close()


async def test_quiet_candidates_do_not_hide_an_unobserved_ninth_item(tmp_path):
    _digest(tmp_path, entries=[_entry(str(i)) for i in range(9)])
    llm, channel = LLM(['{"decision":"skip"}']), Channel()
    store = await Store(str(tmp_path / "roger.db")).open()
    kwargs = dict(client=SimpleNamespace(get_channel=lambda _: channel),
                  settings=_settings(tmp_path), llm=llm, store=store)
    try:
        assert (await run_curated_job(**kwargs))["status"] == "no post-worthy items"
        assert (await run_curated_job(**kwargs))["status"] == "posted"
        assert channel.sent[0][0].url == "https://example.org/8"
        assert llm.calls == 3
    finally:
        await store.close()


async def test_transient_failure_has_one_delayed_retry_per_input_version(tmp_path):
    now = datetime.datetime(2026, 10, 1, 9, tzinfo=datetime.UTC)
    _digest(tmp_path, now=now)
    llm = LLM([APIConnectionError(request=None), APIConnectionError(request=None)])
    store = await Store(str(tmp_path / "roger.db")).open()
    kwargs = dict(client=SimpleNamespace(get_channel=lambda _: Channel()),
                  settings=_settings(tmp_path), llm=llm, store=store)
    try:
        for minutes, expected in [
            (0, "model request failed; skipped"), (59, "unchanged input; skipped"),
            (61, "model request failed; skipped"), (121, "unchanged input; skipped"),
        ]:
            result = await run_curated_job(**kwargs, now=now + datetime.timedelta(minutes=minutes))
            assert result["status"] == expected
        assert llm.calls == 2
    finally:
        await store.close()


async def test_overlapping_checks_share_one_model_observation(tmp_path):
    _digest(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingLLM(LLM):
        async def complete(self, brain, messages, *, curated_review=False):
            if not curated_review:
                entered.set()
                await release.wait()
            return await super().complete(brain, messages, curated_review=curated_review)

    llm, channel = WaitingLLM(), Channel()
    path = str(tmp_path / "roger.db")
    first_store = await Store(path).open()
    second_store = await Store(path).open()
    kwargs = dict(client=SimpleNamespace(get_channel=lambda _: channel),
                  settings=_settings(tmp_path), llm=llm)
    task = asyncio.create_task(run_curated_job(**kwargs, store=first_store))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        overlapping = await asyncio.wait_for(
            run_curated_job(**kwargs, store=second_store), timeout=2,
        )
        assert overlapping["status"] == "observation already in progress"
        release.set()
        assert (await task)["status"] == "posted"
        assert llm.calls == 2
        assert len(channel.sent) == 1
    finally:
        release.set()
        await task
        await first_store.close()
        await second_store.close()


async def test_uncertain_send_uses_capacity_and_spacing_without_retrying_the_item(tmp_path):
    now = datetime.datetime(2026, 10, 1, 9, tzinfo=datetime.UTC)
    _digest(tmp_path, entries=[_entry("a"), _entry("b")], now=now)
    llm, channel = LLM(), Channel(TimeoutError("reply lost"))
    store = await Store(str(tmp_path / "roger.db")).open()
    kwargs = dict(client=SimpleNamespace(get_channel=lambda _: channel),
                  settings=_settings(tmp_path, curated_max_posts_per_day=2), llm=llm, store=store)
    try:
        first = await run_curated_job(**kwargs, now=now)
        early = await run_curated_job(**kwargs, now=now + datetime.timedelta(minutes=30))
        channel.error = None
        second = await run_curated_job(**kwargs, now=now + datetime.timedelta(minutes=61))
        full = await run_curated_job(**kwargs, now=now + datetime.timedelta(hours=3))
        assert first["status"] == "delivery uncertain; manual check required"
        assert early["status"] == "spacing deferred"
        assert second["status"] == "posted" and second["pending_deliveries"] == 1
        assert full["status"] == "delivery uncertain; manual check required"
        assert len(channel.sent) == 2 and llm.calls == 4
        assert {embed.url for embed, _ in channel.sent} == {
            "https://example.org/a", "https://example.org/b",
        }
    finally:
        await store.close()


async def test_observation_ceiling_counts_model_admissions_and_survives_restart(tmp_path):
    now = datetime.datetime(2026, 10, 1, 9, tzinfo=datetime.UTC)
    _digest(tmp_path, entries=[_entry(str(i)) for i in range(9)], now=now)
    settings = _settings(tmp_path, curated_max_observations_per_day=1)
    llm = LLM(['{"decision":"skip"}'])
    path = str(tmp_path / "roger.db")
    store = await Store(path).open()
    kwargs = dict(client=SimpleNamespace(get_channel=lambda _: Channel()),
                  settings=settings, llm=llm)
    try:
        first = await run_curated_job(**kwargs, store=store, now=now)
        assert first["status"] == "no post-worthy items"
        assert first["remaining_observations"] == 0
    finally:
        await store.close()
    store = await Store(path).open()
    try:
        full = await run_curated_job(**kwargs, store=store, now=now + datetime.timedelta(hours=1))
        assert full["status"] == "daily observation limit reached"
        assert llm.calls == 1
        tomorrow = await run_curated_job(
            **kwargs, store=store, now=now + datetime.timedelta(days=1),
        )
        assert tomorrow["status"] == "posted"
        assert llm.calls == 3
    finally:
        await store.close()


async def test_model_policy_change_reconsiders_rejected_input(tmp_path):
    _digest(tmp_path)
    llm = LLM(["not JSON"])
    settings = _settings(tmp_path)
    store = await Store(str(tmp_path / "roger.db")).open()
    kwargs = dict(client=SimpleNamespace(get_channel=lambda _: Channel()),
                  settings=settings, llm=llm, store=store)
    try:
        assert (await run_curated_job(**kwargs))["status"].startswith("unusable model response")
        assert (await run_curated_job(**kwargs))["status"] == "unchanged input; skipped"
        settings.curated_models = ["test/new-draft"]
        assert (await run_curated_job(**kwargs))["status"] == "posted"
        assert llm.calls == 3
    finally:
        await store.close()


@pytest.mark.parametrize("first_time, second_time, third_time, cap", [
    ("2026-10-02T03:30:00+00:00", "2026-10-02T04:30:00+00:00",
     "2026-10-02T05:01:00+00:00", 1),  # local midnight
    ("2026-11-01T05:30:00+00:00", "2026-11-01T06:30:00+00:00",
     "2026-11-01T07:01:00+00:00", 2),  # repeated 01:30
    ("2026-03-08T06:30:00+00:00", "2026-03-08T07:30:00+00:00",
     "2026-03-08T08:01:00+00:00", 2),  # missing 02:30
])
async def test_spacing_uses_elapsed_time_across_midnight_and_dst(
    tmp_path, first_time, second_time, third_time, cap,
):
    first, second, third = [datetime.datetime.fromisoformat(value)
                            for value in (first_time, second_time, third_time)]
    _digest(tmp_path, entries=[_entry("a"), _entry("b")], now=first)
    llm, channel = LLM(), Channel()
    settings = _settings(tmp_path, tz="America/Detroit", curated_max_posts_per_day=cap,
                         curated_min_spacing_minutes=90)
    store = await Store(str(tmp_path / "roger.db")).open()
    kwargs = dict(client=SimpleNamespace(get_channel=lambda _: channel),
                  settings=settings, llm=llm, store=store)
    try:
        assert (await run_curated_job(**kwargs, now=first))["status"] == "posted"
        assert (await run_curated_job(**kwargs, now=second))["status"] == "spacing deferred"
        assert (await run_curated_job(**kwargs, now=third))["status"] == "posted"
        assert llm.calls == 4 and len(channel.sent) == 2
    finally:
        await store.close()


async def test_url_aliases_of_a_delivered_event_do_not_spend_another_model_call(tmp_path):
    first, alias = _entry("a"), _entry("alias")
    alias.link = first.link + "#discussion"
    _digest(tmp_path, entries=[first, alias])
    llm = LLM()
    settings = _settings(tmp_path, curated_max_posts_per_day=3, curated_min_spacing_minutes=0)
    store = await Store(str(tmp_path / "roger.db")).open()
    kwargs = dict(client=SimpleNamespace(get_channel=lambda _: Channel()),
                  settings=settings, llm=llm, store=store)
    try:
        assert (await run_curated_job(**kwargs))["status"] == "posted"
        assert (await run_curated_job(**kwargs))["status"] == "no post-worthy items"
        assert llm.calls == 2
    finally:
        await store.close()


async def test_observation_deadline_cancels_model_work_and_defers_one_retry(tmp_path, monkeypatch):
    _digest(tmp_path)
    monkeypatch.setattr(store_module, "CURATED_CHECK_LEASE_SECONDS", 1)
    monkeypatch.setattr(curated, "CURATED_CHECK_LEASE_SECONDS", 1)

    class SlowLLM(LLM):
        async def complete(self, *args, **kwargs):
            response = await super().complete(*args, **kwargs)
            await asyncio.sleep(2)
            return response

    llm, channel = SlowLLM(), Channel()
    store = await Store(str(tmp_path / "roger.db")).open()
    kwargs = dict(client=SimpleNamespace(get_channel=lambda _: channel),
                  settings=_settings(tmp_path), llm=llm, store=store)
    try:
        first = await run_curated_job(**kwargs)
        assert first["status"] == "model request failed; skipped"
        assert (await run_curated_job(**kwargs))["status"] == "unchanged input; skipped"
        assert llm.calls == 1 and channel.sent == []
    finally:
        await store.close()
