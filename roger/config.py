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
    model_gigabrain: str = ""
    model_spark: str = ""

    # --- budgets (daily in+out tokens per brain) ---
    daily_tokens_admin: int = 150_000
    daily_tokens_ambient: int = 40_000
    daily_tokens_digest: int = 30_000
    daily_tokens_gigabrain: int = 100_000
    daily_tokens_spark: int = 30_000

    # --- budgets (daily USD, layered on top of the token caps above) ---
    # 0 = disabled (opt-in); set to a real figure once OpenRouter cost data looks right for your
    # model mix. The token cap above keeps enforcing regardless — this is an additional, tighter
    # trip wire, not a replacement (a provider that never reports cost would otherwise leave the
    # brain with no effective cap at all).
    daily_usd_admin: float = 0.0
    daily_usd_ambient: float = 0.0
    daily_usd_digest: float = 0.0
    daily_usd_gigabrain: float = 0.0
    daily_usd_spark: float = 0.0

    # --- admin tool loop bounds (§2.9) ---
    admin_max_tool_calls: int = 10
    admin_max_turns: int = 14

    # --- gigabrain tool loop bounds (read-only, own budget) ---
    gigabrain_max_tool_calls: int = 10
    gigabrain_max_turns: int = 14
    # OpenRouter unified `reasoning.effort` (e.g. "high") — sent only if set; opt-in per model.
    gigabrain_reasoning_effort: str = ""

    # --- gigabrain periodic suggestions ---
    gigabrain_interval_days: int = 0  # 0 = disabled; e.g. 7 for weekly
    gigabrain_hour: int = 9
    # unset = DM the owner directly; set = post there instead (same shape as digest_channel_id).
    gigabrain_channel_id: int | None = None

    # --- ambient rate limiting ---
    ambient_rate_per_user: int = 5
    ambient_rate_window_s: int = 600
    ambient_global_hourly: int = 30

    # --- digest ---
    digest_feeds: str = ""
    digest_channel_id: int | None = None
    digest_hour: int = 8

    # --- personal digest (owner-only, DM by default) ---
    personal_digest_feeds: str = ""
    # unset = DM the owner directly; set = post there instead (same shape as digest_channel_id).
    personal_digest_channel_id: int | None = None
    personal_digest_hour: int = 7

    # --- spark (no feed list of its own — reuses digest_feeds). Required channel, no DM
    # fallback: a discussion prompt needs an audience. ---
    spark_channel_id: int | None = None
    spark_hour: int = 7

    # --- ops ---
    # where Roger posts its boot self-report; None disables the report (logs still fire).
    ops_channel_id: int | None = None

    # --- observability ---
    # Prometheus /metrics port (bound inside the container); 0 disables the endpoint.
    metrics_port: int = 9108

    # --- runtime ---
    tz: str = "America/Detroit"
    db_path: str = "/data/roger.db"
    log_level: str = "INFO"

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
