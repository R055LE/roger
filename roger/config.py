"""Typed settings loaded from the environment (pydantic-settings).

Every value comes from the process environment, injected at runtime via ``sops exec-env``.
Nothing is read from a committed file — see the security posture in the README.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    # ``protected_namespaces=()`` lets us keep the spec's MODEL_* names without pydantic
    # complaining about the ``model_`` prefix.
    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
        protected_namespaces=(),
    )

    # --- core / required ---
    discord_token: str = Field(min_length=1)
    openrouter_api_key: str = Field(min_length=1)
    owner_id: int = Field(gt=0)
    guild_id: int = Field(gt=0)

    # --- llm ---
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model_admin: str = ""
    model_ambient: str = ""
    model_digest: str = ""
    model_gigabrain: str = ""
    model_spark: str = ""

    # --- budgets (daily in+out tokens per brain) ---
    daily_tokens_admin: int = Field(default=150_000, gt=0)
    daily_tokens_ambient: int = Field(default=40_000, gt=0)
    daily_tokens_digest: int = Field(default=30_000, gt=0)
    daily_tokens_gigabrain: int = Field(default=100_000, gt=0)
    daily_tokens_spark: int = Field(default=30_000, gt=0)

    # --- budgets (daily USD, layered on top of the token caps above) ---
    # 0 = disabled (opt-in); set to a real figure once OpenRouter cost data looks right for your
    # model mix. The token cap above keeps enforcing regardless — this is an additional, tighter
    # trip wire, not a replacement (a provider that never reports cost would otherwise leave the
    # brain with no effective cap at all).
    daily_usd_admin: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    daily_usd_ambient: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    daily_usd_digest: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    daily_usd_gigabrain: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    daily_usd_spark: float = Field(default=0.0, ge=0, allow_inf_nan=False)

    # --- admin tool loop bounds (§2.9) ---
    admin_max_tool_calls: int = Field(default=10, gt=0)
    admin_max_turns: int = Field(default=14, gt=0)

    # --- gigabrain tool loop bounds (read-only, own budget) ---
    gigabrain_max_tool_calls: int = Field(default=10, gt=0)
    gigabrain_max_turns: int = Field(default=14, gt=0)
    # OpenRouter unified `reasoning.effort` (e.g. "high") — sent only if set; opt-in per model.
    gigabrain_reasoning_effort: str = ""

    # --- gigabrain periodic suggestions ---
    gigabrain_interval_days: int = Field(default=0, ge=0)  # 0 = disabled; e.g. 7 for weekly
    gigabrain_hour: int = Field(default=9, ge=0, le=23)
    # unset = DM the owner directly; set = post there instead (same shape as digest_channel_id).
    gigabrain_channel_id: int | None = Field(default=None, gt=0)

    # --- ambient rate limiting ---
    ambient_rate_per_user: int = Field(default=5, gt=0)
    ambient_rate_window_s: int = Field(default=600, gt=0)
    ambient_global_hourly: int = Field(default=30, gt=0)

    # --- digest ---
    digest_feeds: str = ""
    digest_channel_id: int | None = Field(default=None, gt=0)
    digest_hour: int = Field(default=8, ge=0, le=23)

    # --- personal digest (owner-only, DM by default) ---
    personal_digest_feeds: str = ""
    # unset = DM the owner directly; set = post there instead (same shape as digest_channel_id).
    personal_digest_channel_id: int | None = Field(default=None, gt=0)
    personal_digest_hour: int = Field(default=7, ge=0, le=23)

    # --- spark (no feed list of its own — reuses digest_feeds). Required channel, no DM
    # fallback: a discussion prompt needs an audience. ---
    spark_channel_id: int | None = Field(default=None, gt=0)
    spark_hour: int = Field(default=7, ge=0, le=23)

    # --- ops ---
    # where Roger posts its boot self-report; None disables the report (logs still fire).
    ops_channel_id: int | None = Field(default=None, gt=0)

    # --- observability ---
    # Prometheus /metrics port (bound inside the container); 0 disables the endpoint.
    metrics_port: int = Field(default=9108, ge=0, le=65535)

    # --- runtime ---
    tz: str = "America/Detroit"
    db_path: str = "/data/roger.db"
    log_level: str = "INFO"

    @field_validator("discord_token", "openrouter_api_key", mode="before")
    @classmethod
    def _secret_must_not_be_blank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator(
        "digest_channel_id",
        "ops_channel_id",
        "gigabrain_channel_id",
        "personal_digest_channel_id",
        "spark_channel_id",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        # compose interpolation yields "" for an unset optional int; treat it as absent.
        if value in ("", None):
            return None
        return value

    @field_validator("tz")
    @classmethod
    def _timezone_must_be_loadable(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("must name a loadable IANA timezone") from exc
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
    def gigabrain_models(self) -> list[str]:
        return _split_csv(self.model_gigabrain)

    @property
    def spark_models(self) -> list[str]:
        return _split_csv(self.model_spark)

    @property
    def feeds(self) -> list[str]:
        return _split_csv(self.digest_feeds)

    @property
    def personal_feeds(self) -> list[str]:
        return _split_csv(self.personal_digest_feeds)


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
