"""Typed settings loaded from the environment (pydantic-settings).

Every value comes from the process environment, injected at runtime via ``sops exec-env``.
Nothing is read from a committed file — see the security posture in the README.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    # ``protected_namespaces=()`` lets us keep the spec's MODEL_* names without pydantic
    # complaining about the ``model_`` prefix.
    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        protected_namespaces=(),
    )

    # --- core / required ---
    discord_token: str
    openrouter_api_key: str
    owner_id: int
    guild_id: int

    # --- llm ---
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model_admin: str = ""
    model_ambient: str = ""
    model_digest: str = ""

    # --- budgets (daily in+out tokens per brain) ---
    daily_tokens_admin: int = 50_000
    daily_tokens_ambient: int = 30_000
    daily_tokens_digest: int = 20_000

    # --- ambient rate limiting ---
    ambient_rate_per_user: int = 5
    ambient_rate_window_s: int = 600
    ambient_global_hourly: int = 30

    # --- digest ---
    digest_feeds: str = ""
    digest_channel_id: int | None = None
    digest_hour: int = 8

    # --- runtime ---
    tz: str = "America/Detroit"
    db_path: str = "/data/roger.db"
    log_level: str = "INFO"

    @field_validator("digest_channel_id", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        # compose interpolation yields "" for an unset optional int; treat it as absent.
        if value in ("", None):
            return None
        return value

    @property
    def admin_models(self) -> list[str]:
        return _split_csv(self.model_admin)

    @property
    def ambient_models(self) -> list[str]:
        return _split_csv(self.model_ambient)

    @property
    def digest_models(self) -> list[str]:
        return _split_csv(self.model_digest)

    @property
    def feeds(self) -> list[str]:
        return _split_csv(self.digest_feeds)


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
