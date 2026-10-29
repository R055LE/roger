"""LLM wrapper gates — config + budget checks, retry policy, and usage/cost recording."""

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from openai.types.chat import ChatCompletion

from roger import curated_protocol, story_eval
from roger.brains import stories
from roger.config import Settings
from roger.llm import (
    LLM,
    MAX_ATTEMPTS,
    BudgetExceeded,
    CuratedModelError,
    LLMConfigError,
    _retry_after_seconds,
    curated_workflow,
)
from roger.store import Store


async def _no_sleep(*_args, **_kwargs):
    return None


_REQUEST = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")

_REQUIRED = {
    "DISCORD_TOKEN": "x",
    "OPENROUTER_API_KEY": "y",
    "OWNER_ID": "1",
    "GUILD_ID": "2",
}


def _env(monkeypatch, **extra):
    if extra.get("CURATED_REQUEST_PROFILE") == "sol-astra-v1":
        extra = {"DAILY_USD_CURATED": "2", "DAILY_ATTEMPTS_CURATED": "8",
                 "DAILY_WORKFLOWS_CURATED": "2", **extra}
    for key, value in {**_REQUIRED, **extra}.items():
        monkeypatch.setenv(key, value)


async def test_config_error_when_no_models(monkeypatch, tmp_path):
    _env(monkeypatch)  # MODEL_ADMIN unset
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)
        with pytest.raises(LLMConfigError):
            await llm.complete("admin", [{"role": "user", "content": "hi"}])
    finally:
        await store.close()


async def test_curated_review_requires_its_own_model(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_CURATED="a/b")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)
        with pytest.raises(LLMConfigError, match="MODEL_CURATED_REVIEW"):
            await llm.complete("curated", [{"role": "user", "content": "hi"}],
                               curated_review=True)
    finally:
        await store.close()


async def test_public_curated_without_reviewer_is_flagged_at_preflight(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_CURATED="a/b", CURATED_CHANNEL_ID="42")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)

        async def fake_get(path, **_kwargs):
            if path == "/key":
                return {"data": {}}
            return {"data": [{"id": "a/b"}]}

        monkeypatch.setattr(llm._client, "get", fake_get)
        assert any("MODEL_CURATED_REVIEW" in p for p in await llm.preflight())
    finally:
        await store.close()


async def test_curated_review_uses_its_model_and_shared_budget(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_CURATED="a/b", MODEL_CURATED_REVIEW="c/d",
         DAILY_TOKENS_CURATED="10")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)
        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=6, completion_tokens=4, cost=0.01)
            )

        monkeypatch.setattr(llm._client.chat.completions, "create", fake_create)
        await llm.complete("curated", [{"role": "user", "content": "hi"}],
                           curated_review=True)
        assert captured["model"] == "c/d"
        assert captured["extra_body"]["models"] == ["c/d"]
        assert await store.usage_today("curated") == 10
        assert await store.cost_today("curated") == 0.01
        with pytest.raises(BudgetExceeded):
            await llm.complete("curated", [{"role": "user", "content": "hi"}])
    finally:
        await store.close()


async def test_curated_story_output_ceiling_is_scoped_and_uses_shared_budget(
    monkeypatch, tmp_path,
):
    _env(monkeypatch, MODEL_CURATED="a/b", MODEL_CURATED_REVIEW="c/d")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)
        calls = []

        async def fake_create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=6, completion_tokens=4, cost=0.01)
            )

        monkeypatch.setattr(llm._client.chat.completions, "create", fake_create)
        messages = [{"role": "user", "content": "hi"}]
        await llm.complete("curated", messages)
        await llm.complete("curated", messages, curated_story=True)
        await llm.complete("curated", messages, curated_review=True)

        assert [call["max_tokens"] for call in calls] == [900, 4096, 900]
        assert [call["extra_body"]["models"] for call in calls] == [
            ["a/b"], ["a/b"], ["c/d"],
        ]
        assert all("response_format" not in call for call in calls)
        assert await store.usage_today("curated") == 30
        assert await store.cost_today("curated") == pytest.approx(0.03)
    finally:
        await store.close()


@pytest.mark.parametrize(
    ("brain", "options", "message"),
    [
        ("admin", {"curated_story": True}, "only available to the curated brain"),
        (
            "curated",
            {"curated_story": True, "curated_review": True},
            "not available to review calls",
        ),
    ],
)
async def test_curated_story_output_ceiling_rejects_other_purposes(
    monkeypatch, tmp_path, brain, options, message,
):
    _env(monkeypatch, MODEL_ADMIN="a/b", MODEL_CURATED="a/b", MODEL_CURATED_REVIEW="c/d")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)
        with pytest.raises(ValueError, match=message):
            await llm.complete(brain, [{"role": "user", "content": "hi"}], **options)
    finally:
        await store.close()


@pytest.mark.parametrize(
    ("brain", "options", "tools", "message"),
    [
        ("admin", {"curated_story": True}, None, "only available to the curated brain"),
        ("curated", {}, None, "only available to curated story or review calls"),
        (
            "curated", {"curated_story": True, "curated_review": True}, None,
            "not available to review calls",
        ),
        ("curated", {"curated_story": True}, [{"type": "function"}], "not available with tools"),
    ],
)
async def test_response_format_rejects_other_purposes_and_tools(
    monkeypatch, tmp_path, brain, options, tools, message,
):
    _env(monkeypatch, MODEL_ADMIN="a/b", MODEL_CURATED="a/b", MODEL_CURATED_REVIEW="c/d")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)
        with pytest.raises(ValueError, match=message):
            await llm.complete(
                brain,
                [{"role": "user", "content": "hi"}],
                tools=tools,
                response_format={"type": "json_schema"},
                **options,
            )
    finally:
        await store.close()


async def test_sdk_request_serializes_strict_curated_formats_and_provider_filter(
    monkeypatch, tmp_path,
):
    _env(monkeypatch, MODEL_CURATED="draft/model,fallback/model",
         MODEL_CURATED_REVIEW="review/model")
    store = await Store(str(tmp_path / "l.db")).open()
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0,
            "model": requests[-1]["model"],
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "{}"},
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    try:
        llm = LLM(Settings(), store)
        await llm._client.close()
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        llm._client = AsyncOpenAI(
            api_key="test", base_url="https://openrouter.test/api/v1",
            max_retries=0, http_client=http_client,
        )
        messages = [{"role": "user", "content": "hi"}]
        await llm.complete(
            "curated", messages, curated_story=True,
            response_format=stories.DECISION_RESPONSE_FORMAT,
        )
        await llm.complete(
            "curated", messages, curated_review=True,
            response_format=stories.REVIEW_RESPONSE_FORMAT,
        )

        assert [request["model"] for request in requests] == ["draft/model", "review/model"]
        assert [request["models"] for request in requests] == [
            ["draft/model", "fallback/model"], ["review/model"],
        ]
        assert [request["max_tokens"] for request in requests] == [4096, 900]
        assert [request["response_format"] for request in requests] == [
            stories.DECISION_RESPONSE_FORMAT, stories.REVIEW_RESPONSE_FORMAT,
        ]
        assert all(
            request["provider"] == {"require_parameters": True} for request in requests
        )
    finally:
        if "llm" in locals():
            await llm._client.close()
        await store.close()


async def test_budget_exceeded_before_network(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_ADMIN="a/b", DAILY_TOKENS_ADMIN="10")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        await store.add_usage("admin", 8, 5)  # 13 >= 10
        llm = LLM(Settings(), store)
        with pytest.raises(BudgetExceeded) as exc_info:
            await llm.complete("admin", [{"role": "user", "content": "hi"}])
        assert exc_info.value.unit == "tokens"
    finally:
        await store.close()


async def test_usd_budget_exceeded_before_network(monkeypatch, tmp_path):
    # Token cap (default 150k) is nowhere near tripped — only the $ cap should fire.
    _env(monkeypatch, MODEL_ADMIN="a/b", DAILY_USD_ADMIN="1.0")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        await store.add_usage("admin", 1, 1, cost_usd=1.5)  # $1.50 >= $1.00 cap
        llm = LLM(Settings(), store)
        with pytest.raises(BudgetExceeded) as exc_info:
            await llm.complete("admin", [{"role": "user", "content": "hi"}])
        assert exc_info.value.unit == "usd"
    finally:
        await store.close()


async def test_usd_cap_does_not_trip_below_threshold(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_ADMIN="a/b", DAILY_USD_ADMIN="1.0")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        await store.add_usage("admin", 1, 1, cost_usd=0.5)  # $0.50 < $1.00 cap
        llm = LLM(Settings(), store)

        async def fake_create(**kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, cost=0.1)
            )

        monkeypatch.setattr(llm._client.chat.completions, "create", fake_create)
        await llm.complete("admin", [{"role": "user", "content": "hi"}])  # does not raise
    finally:
        await store.close()


async def test_records_tokens_and_openrouter_cost(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_ADMIN="a/b")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)

        async def fake_create(**kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50, cost=0.0075)
            )

        monkeypatch.setattr(llm._client.chat.completions, "create", fake_create)

        await llm.complete("admin", [{"role": "user", "content": "hi"}])
        assert await store.usage_today("admin") == 150
        assert abs(await store.cost_today("admin") - 0.0075) < 1e-9
    finally:
        await store.close()


async def test_gigabrain_budget_exceeded_before_network(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_GIGABRAIN="a/b", DAILY_TOKENS_GIGABRAIN="10")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        await store.add_usage("gigabrain", 8, 5)  # 13 >= 10
        llm = LLM(Settings(), store)
        with pytest.raises(BudgetExceeded):
            await llm.complete("gigabrain", [{"role": "user", "content": "hi"}])
    finally:
        await store.close()


async def test_gigabrain_reasoning_effort_is_passed_through_when_set(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_GIGABRAIN="a/b", GIGABRAIN_REASONING_EFFORT="high")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)
        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, cost=0.0)
            )

        monkeypatch.setattr(llm._client.chat.completions, "create", fake_create)
        await llm.complete("gigabrain", [{"role": "user", "content": "hi"}])
        assert captured["extra_body"]["reasoning"] == {"effort": "high"}
    finally:
        await store.close()


async def test_admin_has_no_reasoning_effort_by_default(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_ADMIN="a/b")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)
        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, cost=0.0)
            )

        monkeypatch.setattr(llm._client.chat.completions, "create", fake_create)
        await llm.complete("admin", [{"role": "user", "content": "hi"}])
        assert "reasoning" not in captured["extra_body"]
    finally:
        await store.close()


def test_retry_after_parses_integer_seconds():
    exc = SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "2"}))
    assert _retry_after_seconds(exc) == 2.0


def test_retry_after_none_when_missing_response_or_header_or_date():
    assert _retry_after_seconds(SimpleNamespace(response=None)) is None
    assert _retry_after_seconds(SimpleNamespace(response=SimpleNamespace(headers={}))) is None
    http_date = SimpleNamespace(headers={"retry-after": "Wed, 21 Oct 2025 07:28:00 GMT"})
    assert _retry_after_seconds(SimpleNamespace(response=http_date)) is None


async def test_transient_error_retries_then_succeeds(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_ADMIN="a/b")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)
        calls = {"n": 0}

        async def flaky(**_kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise APIConnectionError(request=_REQUEST)
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, cost=0.0)
            )

        monkeypatch.setattr(llm._client.chat.completions, "create", flaky)
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)  # no real backoff wait in tests

        await llm.complete("admin", [{"role": "user", "content": "hi"}])
        assert calls["n"] == 3  # failed twice, third attempt succeeded
        assert await store.usage_today("admin") == 2  # only the successful call records usage
    finally:
        await store.close()


async def test_retries_exhausted_reraises(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_ADMIN="a/b")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)
        calls = {"n": 0}

        async def always_fail(**_kwargs):
            calls["n"] += 1
            raise APIConnectionError(request=_REQUEST)

        monkeypatch.setattr(llm._client.chat.completions, "create", always_fail)
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)

        with pytest.raises(APIConnectionError):
            await llm.complete("admin", [{"role": "user", "content": "hi"}])
        assert calls["n"] == MAX_ATTEMPTS  # bounded, not infinite
    finally:
        await store.close()


async def test_missing_cost_field_defaults_to_zero(monkeypatch, tmp_path):
    # A vanilla OpenAI-compatible host won't return `cost`; usage still records, cost stays 0.
    _env(monkeypatch, MODEL_ADMIN="a/b")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)

        async def fake_create(**kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10)  # no `cost`
            )

        monkeypatch.setattr(llm._client.chat.completions, "create", fake_create)

        await llm.complete("admin", [{"role": "user", "content": "hi"}])
        assert await store.usage_today("admin") == 30
        assert await store.cost_today("admin") == 0.0
    finally:
        await store.close()


async def test_preflight_ok_when_key_and_all_configured_models_resolve(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_ADMIN="a/b", MODEL_AMBIENT="a/b,c/d",
         MODEL_CURATED_REVIEW="e/f")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)

        async def fake_get(path, **_kwargs):
            if path == "/key":
                return {"data": {"label": "test"}}
            assert path == "/models"
            return {"data": [{"id": "a/b"}, {"id": "c/d"}, {"id": "e/f"}]}

        monkeypatch.setattr(llm._client, "get", fake_get)
        assert await llm.preflight() == []
    finally:
        await store.close()


async def test_preflight_flags_a_rejected_key(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_ADMIN="a/b")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)
        resp = httpx.Response(401, request=_REQUEST)

        async def fake_get(path, **_kwargs):
            if path == "/key":
                raise APIStatusError("Error code: 401", response=resp, body=None)
            return {"data": [{"id": "a/b"}]}

        monkeypatch.setattr(llm._client, "get", fake_get)
        problems = await llm.preflight()
        assert any("OpenRouter key rejected" in p for p in problems)
    finally:
        await store.close()


async def test_preflight_flags_a_key_check_connection_failure(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_ADMIN="a/b")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)

        async def fake_get(path, **_kwargs):
            if path == "/key":
                raise APIConnectionError(request=_REQUEST)
            return {"data": [{"id": "a/b"}]}

        monkeypatch.setattr(llm._client, "get", fake_get)
        problems = await llm.preflight()
        assert any("OpenRouter key check failed" in p for p in problems)
    finally:
        await store.close()


async def test_preflight_flags_a_model_not_in_the_catalog(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_ADMIN="a/bogus,a/b")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)

        async def fake_get(path, **_kwargs):
            if path == "/key":
                return {"data": {}}
            return {"data": [{"id": "a/b"}]}

        monkeypatch.setattr(llm._client, "get", fake_get)
        problems = await llm.preflight()
        assert len(problems) == 1
        assert "MODEL_ADMIN" in problems[0] and "a/bogus" in problems[0]
    finally:
        await store.close()


async def test_preflight_flags_a_model_catalog_fetch_failure(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_ADMIN="a/b")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)

        async def fake_get(path, **_kwargs):
            if path == "/key":
                return {"data": {}}
            raise APIConnectionError(request=_REQUEST)

        monkeypatch.setattr(llm._client, "get", fake_get)
        problems = await llm.preflight()
        assert any("model catalog fetch failed" in p for p in problems)
    finally:
        await store.close()


async def test_preflight_skips_the_catalog_call_when_nothing_is_configured(monkeypatch, tmp_path):
    _env(monkeypatch)  # no MODEL_* set for any brain
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        llm = LLM(Settings(), store)
        calls = []

        async def fake_get(path, **_kwargs):
            calls.append(path)
            return {"data": {}}

        monkeypatch.setattr(llm._client, "get", fake_get)
        assert await llm.preflight() == []
        assert calls == ["/key"]  # /models never fetched — nothing to check it against
    finally:
        await store.close()


async def test_curated_budget_exceeded_before_network(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_CURATED="a/b", DAILY_TOKENS_CURATED="10")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        await store.add_usage("curated", 8, 5)  # 13 >= 10
        llm = LLM(Settings(), store)
        with pytest.raises(BudgetExceeded):
            await llm.complete("curated", [{"role": "user", "content": "hi"}])
    finally:
        await store.close()


@pytest.mark.parametrize("review", [False, True])
@curated_workflow()
async def test_selected_curated_profile_uses_evaluated_output_limits(monkeypatch, tmp_path, review):
    _env(monkeypatch, MODEL_CURATED="legacy/writer", MODEL_CURATED_REVIEW="legacy/reviewer",
         CURATED_REQUEST_PROFILE="sol-astra-v1", CURATED_DEVELOPING_STORIES="true")
    store = await Store(str(tmp_path / "profile.db")).open()
    try:
        llm = LLM(Settings(), store)
        calls = []

        async def create(**kwargs):
            calls.append(kwargs)
            return ChatCompletion.model_validate(_profile_response(review=review))

        monkeypatch.setattr(llm._client.chat.completions, "create", create)
        await llm.complete(
            "curated", [{"role": "user", "content": "one measured development"}],
            curated_story=not review, curated_review=review,
            response_format=(stories.REVIEW_RESPONSE_FORMAT if review
                             else stories.DECISION_RESPONSE_FORMAT),
        )
        assert calls[0]["max_tokens"] == (8192 if review else 16384)
    finally:
        await store.close()


def _profile_response(*, review=False, content="{}"):
    profile = curated_protocol.CURATED_REVIEWER if review else curated_protocol.CURATED_WRITER
    return {
        "id": "gen-profile-fixture", "object": "chat.completion", "created": 1,
        "model": profile.returned_models[-1], "service_tier": "default",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                  "cost": 0.01, "completion_tokens_details": {"reasoning_tokens": 10}},
        "openrouter_metadata": {
            "requested": profile.model, "strategy": "direct", "attempt": 1,
            "attempts": [{"provider": "OpenAI"}],
            "endpoints": {"available": [{"provider": "OpenAI", "model": profile.model,
                                          "selected": True}]}, "pipeline": [],
        },
    }


def test_production_curated_profiles_match_evaluated_contract():
    profiles = story_eval.load_profiles()
    for production in (curated_protocol.CURATED_WRITER, curated_protocol.CURATED_REVIEWER):
        evaluated = profiles[production.name]
        for field in ("model", "returned_models", "provider", "provider_display_name",
                      "service_tier", "reasoning_effort", "prompt_cache",
                      "prompt_usd_per_million", "completion_usd_per_million",
                      "prompt_cache_write_usd_per_million"):
            assert getattr(production, field) == getattr(evaluated, field)
    assert curated_protocol.CURATED_WRITER.max_tokens == profiles["sol"].writer_max_tokens
    assert curated_protocol.CURATED_REVIEWER.max_tokens == profiles["astra"].reviewer_max_tokens


@pytest.mark.parametrize("review", [False, True])
@curated_workflow()
async def test_production_profile_serializes_exact_request(monkeypatch, tmp_path, review):
    _env(monkeypatch, CURATED_REQUEST_PROFILE="sol-astra-v1", CURATED_DEVELOPING_STORIES="true")
    store = await Store(str(tmp_path / "wire.db")).open()
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json=_profile_response(review=review))

    llm = LLM(Settings(), store)
    await llm._client.close()
    llm._client = AsyncOpenAI(
        api_key="test", base_url="https://openrouter.ai/api/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    try:
        await llm.complete(
            "curated", [{"role": "user", "content": "a supported finding"}],
            curated_story=not review, curated_review=review,
            response_format=(stories.REVIEW_RESPONSE_FORMAT if review
                             else stories.DECISION_RESPONSE_FORMAT),
        )
        profile = curated_protocol.CURATED_REVIEWER if review else curated_protocol.CURATED_WRITER
        body = json.loads(requests[0].content)
        assert len(requests) == 1
        assert body["model"] == profile.model
        assert body["max_tokens"] == (8192 if review else 16384)
        assert body["service_tier"] == "default"
        assert body["reasoning"] == {"effort": "medium", "exclude": True}
        assert body["provider"] == {
            "only": ["openai"], "allow_fallbacks": False, "require_parameters": True,
            "max_price": {"prompt": float(profile.prompt_usd_per_million),
                          "completion": float(profile.completion_usd_per_million)},
        }
        assert body["prompt_cache_options"] == {"mode": "explicit"}
        assert body["response_format"]["json_schema"]["strict"] is True
        assert not {"temperature", "top_p", "models", "tools"}.intersection(body)
        assert requests[0].headers["x-openrouter-cache"] == "false"
        assert requests[0].headers["x-openrouter-metadata"] == "enabled"
        assert requests[0].extensions["timeout"]["read"] == profile.timeout
        assert await store.usage_today("curated") == 150
        assert await store.cost_today("curated") == 0.01
        diagnostic = json.loads(await store.get_meta("curated_profile_last_call"))
        assert diagnostic["status"] == "completed"
        assert diagnostic["usage_details"]["cache_write_tokens"] is None
        assert "content" not in diagnostic
    finally:
        await llm._client.close()
        await store.close()


@pytest.mark.parametrize("kind,status", [
    ("null_choices", "malformed_response"), ("missing_usage", "missing_usage_or_cost"),
    ("empty_choices", "malformed_response"),
    ("cache_write", "prompt_cache_policy_mismatch"), ("wrong_model", "model_mismatch"),
    ("blank", "blank_response"),
])
@curated_workflow()
async def test_profile_response_failures_preserve_safe_usage(
    monkeypatch, tmp_path, kind, status,
):
    _env(monkeypatch, CURATED_REQUEST_PROFILE="sol-astra-v1", CURATED_DEVELOPING_STORIES="true")
    store = await Store(str(tmp_path / "failure.db")).open()
    llm = LLM(Settings(), store)
    payload = _profile_response(content="private source text")
    if kind == "null_choices":
        payload["choices"] = None
    elif kind == "empty_choices":
        payload["choices"] = []
    elif kind == "missing_usage":
        del payload["usage"]
    elif kind == "cache_write":
        payload["usage"]["prompt_tokens_details"] = {"cached_tokens": 0, "cache_write_tokens": 10}
    elif kind == "wrong_model":
        payload["model"] = "unexpected/private-model"
    elif kind == "blank":
        payload["choices"][0]["message"]["content"] = ""
    response = ChatCompletion.model_validate({**payload, "choices": payload["choices"] or []})
    if kind == "null_choices":
        response.choices = None

    async def create(**kwargs):
        return response

    monkeypatch.setattr(llm._client.chat.completions, "create", create)
    try:
        with pytest.raises(CuratedModelError, match=status):
            await llm.complete("curated", [{"role": "user", "content": "private source text"}],
                               curated_story=True, response_format=stories.DECISION_RESPONSE_FORMAT)
        diagnostic = await store.get_meta("curated_profile_last_call")
        assert json.loads(diagnostic)["status"] == status
        assert "private source text" not in diagnostic
        assert "unexpected/private-model" not in diagnostic
        uncertain = kind in {"missing_usage", "null_choices", "empty_choices", "wrong_model"}
        assert await store.cost_today("curated") == (0 if uncertain else 0.01)
        state = await store.curated_budget_state()
        assert state["unknown_attempts"] == int(uncertain)
    finally:
        await llm._client.close()
        await store.close()


@curated_workflow()
async def test_profile_transport_has_one_attempt_and_sanitized_uncertainty(monkeypatch, tmp_path):
    _env(monkeypatch, CURATED_REQUEST_PROFILE="sol-astra-v1", CURATED_DEVELOPING_STORIES="true")
    store = await Store(str(tmp_path / "transport.db")).open()
    llm = LLM(Settings(), store)
    calls = 0

    async def create(**kwargs):
        nonlocal calls
        calls += 1
        raise APIConnectionError(message="private vendor response", request=_REQUEST)

    monkeypatch.setattr(llm._client.chat.completions, "create", create)
    try:
        with pytest.raises(CuratedModelError, match="request_error_uncertain") as caught:
            await llm.complete("curated", [{"role": "user", "content": "finding"}],
                               curated_story=True, response_format=stories.DECISION_RESPONSE_FORMAT)
        assert calls == 1
        assert caught.value.__suppress_context__ is True
        diagnostic = await store.get_meta("curated_profile_last_call")
        assert "private vendor response" not in diagnostic
        assert json.loads(diagnostic)["usage"] is None
        assert await store.cost_today("curated") == 0
    finally:
        await llm._client.close()
        await store.close()


async def test_selected_profile_rejects_legacy_curated_before_io(monkeypatch, tmp_path):
    _env(monkeypatch, CURATED_REQUEST_PROFILE="sol-astra-v1", CURATED_DEVELOPING_STORIES="true")
    store = await Store(str(tmp_path / "legacy.db")).open()
    llm = LLM(Settings(), store)
    try:
        with pytest.raises(LLMConfigError, match="tool-free story request"):
            await llm.complete("curated", [{"role": "user", "content": "legacy draft"}])
        assert await store.get_meta("curated_profile_last_call") is None
    finally:
        await llm._client.close()
        await store.close()


async def test_profile_preflight_checks_effective_pair_not_legacy_chains(monkeypatch, tmp_path):
    _env(monkeypatch, CURATED_REQUEST_PROFILE="sol-astra-v1", CURATED_DEVELOPING_STORIES="true",
         CURATED_CHANNEL_ID="42", MODEL_CURATED="legacy/writer", MODEL_CURATED_REVIEW="")
    store = await Store(str(tmp_path / "preflight.db")).open()
    llm = LLM(Settings(), store)

    async def get(path, **kwargs):
        if path == "/key":
            return {"data": {}}
        return {"data": [{"id": profile.model} for profile in (
            curated_protocol.CURATED_WRITER, curated_protocol.CURATED_REVIEWER,
        )]}

    monkeypatch.setattr(llm._client, "get", get)
    try:
        assert await llm.preflight() == []
    finally:
        await llm._client.close()
        await store.close()


async def _profile_call(llm, *, content="finding"):
    return await llm.complete(
        "curated", [{"role": "user", "content": content}], curated_story=True,
        response_format=stories.DECISION_RESPONSE_FORMAT,
    )


@pytest.mark.parametrize("gate", ["tokens", "usd"])
@curated_workflow()
async def test_profile_reserves_full_request_before_first_network_call(monkeypatch, tmp_path, gate):
    profile = curated_protocol.CURATED_WRITER
    bound = curated_protocol.input_token_upper_bound(
        [{"role": "user", "content": "finding"}], stories.DECISION_RESPONSE_FORMAT,
    )
    # This would cover ordinary input pricing, but not the declared cache-write price.
    base_cost = (bound * profile.prompt_usd_per_million
                 + profile.max_tokens * profile.completion_usd_per_million) / Decimal(1_000_000)
    _env(monkeypatch, CURATED_REQUEST_PROFILE="sol-astra-v1", CURATED_DEVELOPING_STORIES="true",
         **({"DAILY_TOKENS_CURATED": str(bound + profile.max_tokens - 1)} if gate == "tokens"
            else {"DAILY_USD_CURATED": str(base_cost)}))
    store = await Store(str(tmp_path / "preflight-reservation.db")).open()
    llm = LLM(Settings(), store)

    async def create(**kwargs):
        raise AssertionError("denied reservation must not call provider")

    monkeypatch.setattr(llm._client.chat.completions, "create", create)
    try:
        with pytest.raises(BudgetExceeded, match=f"reservation denied: {gate}"):
            await _profile_call(llm)
        state = await store.curated_budget_state()
        assert state["used_tokens"] == state["attempts_today"] == 0
        diagnostic = json.loads(await store.get_meta("curated_profile_last_call"))
        assert diagnostic["status"] == f"budget_{gate}"
    finally:
        await llm._client.close()
        await store.close()


async def test_profile_rejects_calls_without_a_workflow(monkeypatch, tmp_path):
    _env(monkeypatch, CURATED_REQUEST_PROFILE="sol-astra-v1", CURATED_DEVELOPING_STORIES="true")
    store = await Store(str(tmp_path / "context.db")).open()
    llm = LLM(Settings(), store)
    try:
        with pytest.raises(LLMConfigError, match="requires an editorial workflow"):
            await _profile_call(llm)
        assert (await store.curated_budget_state())["attempts_today"] == 0
    finally:
        await llm._client.close()
        await store.close()


@curated_workflow()
async def test_profile_counts_utf8_and_schema_before_reserving(monkeypatch, tmp_path):
    _env(monkeypatch, CURATED_REQUEST_PROFILE="sol-astra-v1", CURATED_DEVELOPING_STORIES="true")
    store = await Store(str(tmp_path / "input.db")).open()
    llm = LLM(Settings(), store)
    try:
        with pytest.raises(LLMConfigError, match="input reservation maximum"):
            await _profile_call(llm, content="é" * 15_000)
        assert (await store.curated_budget_state())["attempts_today"] == 0
    finally:
        await llm._client.close()
        await store.close()


@curated_workflow()
async def test_profile_fifth_call_in_one_workflow_is_denied(monkeypatch, tmp_path):
    _env(monkeypatch, CURATED_REQUEST_PROFILE="sol-astra-v1", CURATED_DEVELOPING_STORIES="true")
    store = await Store(str(tmp_path / "four-calls.db")).open()
    llm = LLM(Settings(), store)
    calls = 0

    async def create(**kwargs):
        nonlocal calls
        calls += 1
        return ChatCompletion.model_validate(_profile_response())

    monkeypatch.setattr(llm._client.chat.completions, "create", create)
    try:
        for _ in range(4):
            await _profile_call(llm)
        with pytest.raises(BudgetExceeded, match="workflow_attempts"):
            await _profile_call(llm)
        assert calls == 4
        state = await store.curated_budget_state()
        assert state["attempts_today"] == 4
        assert state["workflows_today"] == 1
        assert state["used_tokens"] == 600
    finally:
        await llm._client.close()
        await store.close()


async def test_profile_concurrent_workflows_have_independent_context(monkeypatch, tmp_path):
    _env(monkeypatch, CURATED_REQUEST_PROFILE="sol-astra-v1", CURATED_DEVELOPING_STORIES="true",
         DAILY_TOKENS_CURATED="100000")
    store = await Store(str(tmp_path / "concurrent.db")).open()
    llm = LLM(Settings(), store)
    both_started = asyncio.Event()
    calls = 0

    async def create(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            both_started.set()
        await both_started.wait()
        return ChatCompletion.model_validate(_profile_response())

    async def workflow():
        async with curated_workflow():
            return await _profile_call(llm)

    monkeypatch.setattr(llm._client.chat.completions, "create", create)
    try:
        await asyncio.wait_for(asyncio.gather(workflow(), workflow()), timeout=5)
        assert calls == 2
        state = await store.curated_budget_state()
        assert state["workflows_today"] == state["attempts_today"] == 2
        with pytest.raises(BudgetExceeded, match="daily_workflows"):
            await workflow()
        assert calls == 2
        with pytest.raises(LLMConfigError, match="editorial workflow"):
            await _profile_call(llm)
    finally:
        await llm._client.close()
        await store.close()


async def test_profile_cancellation_keeps_reservation_after_reopen(monkeypatch, tmp_path):
    _env(monkeypatch, CURATED_REQUEST_PROFILE="sol-astra-v1", CURATED_DEVELOPING_STORIES="true")
    path = str(tmp_path / "cancel.db")
    store = await Store(path).open()
    llm = LLM(Settings(), store)
    started = asyncio.Event()

    async def create(**kwargs):
        started.set()
        await asyncio.Future()

    async def workflow():
        async with curated_workflow():
            return await _profile_call(llm)

    monkeypatch.setattr(llm._client.chat.completions, "create", create)
    try:
        task = asyncio.create_task(workflow())
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        diagnostic = json.loads(await store.get_meta("curated_profile_last_call"))
        assert diagnostic["status"] == "request_interrupted_uncertain"
    finally:
        await llm._client.close()
        await store.close()
    reopened = await Store(path).open()
    try:
        state = await reopened.curated_budget_state()
        assert state["unknown_attempts"] == 1
        assert state["unknown_tokens"] > 16_384
        assert state["unknown_usd"] > 0.16384
        assert state["attempts_today"] == state["workflows_today"] == 1
    finally:
        await reopened.close()


@pytest.mark.parametrize("missing_tokens", [False, True])
@curated_workflow()
async def test_profile_reported_overrun_keeps_actual_cost_even_with_partial_usage(
    monkeypatch, tmp_path, missing_tokens,
):
    _env(monkeypatch, CURATED_REQUEST_PROFILE="sol-astra-v1", CURATED_DEVELOPING_STORIES="true")
    store = await Store(str(tmp_path / "overrun.db")).open()
    llm = LLM(Settings(), store)
    response = ChatCompletion.model_validate(_profile_response())
    response.usage.cost = 1.25
    if missing_tokens:
        response.usage.prompt_tokens = None
    calls = 0

    async def create(**kwargs):
        nonlocal calls
        calls += 1
        return response

    monkeypatch.setattr(llm._client.chat.completions, "create", create)
    try:
        with pytest.raises(CuratedModelError, match="reservation_overrun"):
            await _profile_call(llm)
        state = await store.curated_budget_state()
        assert state["unknown_usd"] == 1.25
        assert state["unknown_tokens"] > 16_384
        assert state["used_usd"] == 0
        rows = await store._conn.execute_fetchall(
            "SELECT actual_cost_usd FROM curated_model_attempt",
        )
        assert rows[0][0] == "1.25"
        async with curated_workflow():
            with pytest.raises(BudgetExceeded, match="reservation_overrun"):
                await _profile_call(llm)
        assert calls == 1
    finally:
        await llm._client.close()
        await store.close()
