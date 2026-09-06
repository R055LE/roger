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
_OPTIONAL_CHANNEL_IDS = [
    "DIGEST_CHANNEL_ID",
    "OPS_CHANNEL_ID",
    "GIGABRAIN_CHANNEL_ID",
    "PERSONAL_DIGEST_CHANNEL_ID",
    "SPARK_CHANNEL_ID",
]
_POSITIVE_SETTINGS = [
    "OWNER_ID",
    "GUILD_ID",
    *_OPTIONAL_CHANNEL_IDS,
    "DAILY_TOKENS_ADMIN",
    "DAILY_TOKENS_AMBIENT",
    "DAILY_TOKENS_DIGEST",
    "DAILY_TOKENS_GIGABRAIN",
    "DAILY_TOKENS_SPARK",
    "ADMIN_MAX_TOOL_CALLS",
    "ADMIN_MAX_TURNS",
    "GIGABRAIN_MAX_TOOL_CALLS",
    "GIGABRAIN_MAX_TURNS",
    "AMBIENT_RATE_PER_USER",
    "AMBIENT_RATE_WINDOW_S",
    "AMBIENT_GLOBAL_HOURLY",
]
_USD_CAPS = [
    "DAILY_USD_ADMIN",
    "DAILY_USD_AMBIENT",
    "DAILY_USD_DIGEST",
    "DAILY_USD_GIGABRAIN",
    "DAILY_USD_SPARK",
]
_SCHEDULED_HOURS = [
    "DIGEST_HOUR",
    "PERSONAL_DIGEST_HOUR",
    "SPARK_HOUR",
    "GIGABRAIN_HOUR",
]


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


@pytest.mark.parametrize("setting", _OPTIONAL_CHANNEL_IDS)
def test_empty_optional_channel_id_becomes_none(monkeypatch, setting):
    _set_required(monkeypatch)
    monkeypatch.setenv(setting, "")
    assert getattr(Settings(), setting.lower()) is None


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


def test_missing_required_field_raises(monkeypatch):
    for key in _REQUIRED:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("secret", ["DISCORD_TOKEN", "OPENROUTER_API_KEY"])
@pytest.mark.parametrize("value", ["", "   "])
def test_required_secrets_must_not_be_blank_or_appear_in_validation_output(
    monkeypatch, secret, value
):
    _set_required(monkeypatch)
    monkeypatch.setenv(secret, value)

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    message = str(exc_info.value)
    assert secret.lower() in message
    assert "input_value" not in message
    assert "input_type" not in message


@pytest.mark.parametrize(
    ("secret", "attribute"),
    [("DISCORD_TOKEN", "discord_token"), ("OPENROUTER_API_KEY", "openrouter_api_key")],
)
def test_valid_secret_bytes_are_not_stripped(monkeypatch, secret, attribute):
    _set_required(monkeypatch)
    value = "  secret bytes  "
    monkeypatch.setenv(secret, value)
    assert getattr(Settings(), attribute) == value


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


def test_spark_defaults(monkeypatch):
    _set_required(monkeypatch)
    settings = Settings()
    assert settings.spark_channel_id is None
    assert settings.spark_hour == 7
    assert settings.model_spark == ""
    assert settings.spark_models == []
    assert settings.daily_tokens_spark == 30_000
    assert settings.daily_usd_spark == 0.0


def test_spark_model_chain_is_parsed_to_list(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("MODEL_SPARK", "a/b, c/d")
    assert Settings().spark_models == ["a/b", "c/d"]


@pytest.mark.parametrize("setting", _POSITIVE_SETTINGS)
def test_positive_settings_accept_minimum(monkeypatch, setting):
    _set_required(monkeypatch)
    monkeypatch.setenv(setting, "1")
    Settings()


@pytest.mark.parametrize("setting", _POSITIVE_SETTINGS)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_positive_settings_reject_zero_and_negative_values(monkeypatch, setting, value):
    _set_required(monkeypatch)
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("setting", _USD_CAPS)
@pytest.mark.parametrize("value", ["0", "0.01"])
def test_dollar_caps_accept_disabled_and_positive_values(monkeypatch, setting, value):
    _set_required(monkeypatch)
    monkeypatch.setenv(setting, value)
    Settings()


@pytest.mark.parametrize("setting", _USD_CAPS)
def test_dollar_caps_reject_negative_values(monkeypatch, setting):
    _set_required(monkeypatch)
    monkeypatch.setenv(setting, "-0.01")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("setting", _USD_CAPS)
@pytest.mark.parametrize("value", ["inf", "-inf", "nan"])
def test_dollar_caps_reject_non_finite_values(monkeypatch, setting, value):
    _set_required(monkeypatch)
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("value", ["0", "1"])
def test_gigabrain_interval_accepts_disabled_and_positive_values(monkeypatch, value):
    _set_required(monkeypatch)
    monkeypatch.setenv("GIGABRAIN_INTERVAL_DAYS", value)
    Settings()


def test_gigabrain_interval_rejects_negative_values(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("GIGABRAIN_INTERVAL_DAYS", "-1")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("setting", _SCHEDULED_HOURS)
@pytest.mark.parametrize("hour", ["0", "23"])
def test_scheduled_hours_accept_clock_boundaries(monkeypatch, setting, hour):
    _set_required(monkeypatch)
    monkeypatch.setenv(setting, hour)
    Settings()


@pytest.mark.parametrize("setting", _SCHEDULED_HOURS)
@pytest.mark.parametrize("hour", ["-1", "24"])
def test_scheduled_hours_reject_values_outside_clock(monkeypatch, setting, hour):
    _set_required(monkeypatch)
    monkeypatch.setenv(setting, hour)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("port", ["0", "65535"])
def test_metrics_port_accepts_disable_and_maximum(monkeypatch, port):
    _set_required(monkeypatch)
    monkeypatch.setenv("METRICS_PORT", port)
    Settings()


@pytest.mark.parametrize("port", ["-1", "65536"])
def test_metrics_port_rejects_values_outside_range(monkeypatch, port):
    _set_required(monkeypatch)
    monkeypatch.setenv("METRICS_PORT", port)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("timezone", ["UTC", "America/Detroit"])
def test_timezone_must_be_loadable(monkeypatch, timezone):
    _set_required(monkeypatch)
    monkeypatch.setenv("TZ", timezone)
    assert Settings().tz == timezone


@pytest.mark.parametrize("timezone", ["", "Not/A_Timezone"])
def test_timezone_rejects_unknown_names(monkeypatch, timezone):
    _set_required(monkeypatch)
    monkeypatch.setenv("TZ", timezone)
    with pytest.raises(ValidationError):
        Settings()
