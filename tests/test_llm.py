"""LLM wrapper gates — config + budget checks happen before any network call."""

from types import SimpleNamespace

import pytest

from roger.config import Settings
from roger.llm import LLM, BudgetExceeded, LLMConfigError
from roger.store import Store

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
        with pytest.raises(BudgetExceeded):
            await llm.complete("admin", [{"role": "user", "content": "hi"}])
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
