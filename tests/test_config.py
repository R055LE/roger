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


def test_empty_digest_channel_id_becomes_none(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("DIGEST_CHANNEL_ID", "")
    assert Settings().digest_channel_id is None


def test_missing_required_field_raises(monkeypatch):
    for key in _REQUIRED:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        Settings()
