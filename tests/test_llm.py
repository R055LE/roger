"""LLM wrapper gates — config + budget checks happen before any network call."""

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
