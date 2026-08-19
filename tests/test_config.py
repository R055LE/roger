"""Settings — env parsing, defaults, and required-field enforcement."""

import pytest
from pydantic import ValidationError

from roger.config import Settings

_REQUIRED = {
    "DISCORD_TOKEN": "x",
    "OPENROUTER_API_KEY": "y",
    "OWNER_ID": "1",
    "GUILD_ID": "2",
}


def _set_required(monkeypatch):
    for key, value in _REQUIRED.items():
        monkeypatch.setenv(key, value)


def test_required_fields_and_defaults(monkeypatch):
    _set_required(monkeypatch)
    settings = Settings()
    assert settings.owner_id == 1
    assert settings.guild_id == 2
    assert settings.openrouter_base_url == "https://openrouter.ai/api/v1"
    assert settings.db_path == "/data/roger.db"


def test_model_chain_is_parsed_to_list(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("MODEL_ADMIN", "a/b, c/d ,e/f")
    assert Settings().admin_models == ["a/b", "c/d", "e/f"]


def test_gigabrain_defaults(monkeypatch):
    _set_required(monkeypatch)
    settings = Settings()
    assert settings.gigabrain_models == []
    assert settings.daily_tokens_gigabrain == 100_000
    assert settings.gigabrain_max_tool_calls == 10
    assert settings.gigabrain_max_turns == 14
    assert settings.gigabrain_reasoning_effort == ""


def test_gigabrain_model_chain_is_parsed_to_list(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("MODEL_GIGABRAIN", "a/b, c/d")
    assert Settings().gigabrain_models == ["a/b", "c/d"]


def test_gigabrain_periodic_suggestion_defaults(monkeypatch):
    _set_required(monkeypatch)
    settings = Settings()
    assert settings.gigabrain_interval_days == 0
    assert settings.gigabrain_hour == 9


def test_empty_digest_channel_id_becomes_none(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("DIGEST_CHANNEL_ID", "")
    assert Settings().digest_channel_id is None


def test_personal_digest_defaults(monkeypatch):
    _set_required(monkeypatch)
    settings = Settings()
    assert settings.personal_digest_feeds == ""
    assert settings.personal_feeds == []
    assert settings.personal_digest_channel_id is None
    assert settings.personal_digest_hour == 7


def test_personal_feeds_is_parsed_to_list(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("PERSONAL_DIGEST_FEEDS", "http://a, http://b ,http://c")
    assert Settings().personal_feeds == ["http://a", "http://b", "http://c"]


def test_empty_personal_digest_channel_id_becomes_none(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("PERSONAL_DIGEST_CHANNEL_ID", "")
    assert Settings().personal_digest_channel_id is None


def test_missing_required_field_raises(monkeypatch):
    for key in _REQUIRED:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_daily_usd_defaults_to_disabled(monkeypatch):
    _set_required(monkeypatch)
    settings = Settings()
    assert settings.daily_usd_admin == 0.0
    assert settings.daily_usd_ambient == 0.0
    assert settings.daily_usd_digest == 0.0
    assert settings.daily_usd_gigabrain == 0.0


def test_daily_usd_parses_from_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("DAILY_USD_ADMIN", "2.5")
    assert Settings().daily_usd_admin == 2.5
