"""LLM wrapper gates — config + budget checks, retry policy, and usage/cost recording."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

from roger.config import Settings
from roger.llm import LLM, MAX_ATTEMPTS, BudgetExceeded, LLMConfigError, _retry_after_seconds
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


async def test_spark_budget_exceeded_before_network(monkeypatch, tmp_path):
    _env(monkeypatch, MODEL_SPARK="a/b", DAILY_TOKENS_SPARK="10")
    store = await Store(str(tmp_path / "l.db")).open()
    try:
        await store.add_usage("spark", 8, 5)  # 13 >= 10
        llm = LLM(Settings(), store)
        with pytest.raises(BudgetExceeded):
            await llm.complete("spark", [{"role": "user", "content": "hi"}])
    finally:
        await store.close()
