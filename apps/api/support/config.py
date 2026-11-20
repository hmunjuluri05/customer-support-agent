"""Application settings.

Everything tunable lives here and is surfaced in .env.example. No provider
credential is ever read from, or written to, model context.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------- database --

    database_url: str = Field(
        default="postgresql+psycopg://localhost/support_agent",
        description="SQLAlchemy async URL. Supplied as a Container Apps secret in deployment.",
    )
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_command_timeout_s: float = 10.0

    # ------------------------------------------------------- Azure OpenAI --

    azure_openai_endpoint: str = Field(
        default="",
        description="e.g. https://YOUR-RESOURCE.openai.azure.com/",
    )
    azure_openai_chat_deployment: str = "gpt-chat"
    azure_openai_embedding_deployment: str = "text-embedding-3-small"
    azure_openai_realtime_deployment: str = "gpt-realtime-mini"
    azure_openai_api_version: str = "2025-04-01-preview"

    # Selects the user-assigned identity for DefaultAzureCredential in Azure.
    # Unset locally, where developer credentials are used instead.
    azure_client_id: str | None = None

    # Local development fallback only. Never set in deployment: the container
    # authenticates with managed identity.
    azure_openai_api_key: str | None = None

    # ------------------------------------------------------------ retrieval --

    grounding_threshold: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum COSINE SIMILARITY, computed as 1 - cosine_distance, for a "
            "GROUNDED decision. Higher is stricter. Calibrated on fixtures/eval."
        ),
    )
    grounding_weak_margin: float = Field(
        default=0.08,
        ge=0.0,
        le=0.5,
        description=(
            "Band below the threshold treated as WEAK (ask a clarifying question) "
            "rather than UNSUPPORTED (escalate). Calibrated alongside the threshold."
        ),
    )
    retrieval_top_k: int = 5
    chunk_target_tokens: int = 300
    chunk_overlap_tokens: int = 50
    embedding_dimensions: int = 1536

    # ------------------------------------------------------------- limits --

    max_tool_calls_per_turn: int = 5
    turn_budget_s: float = 30.0
    tool_timeout_s: float = 8.0
    llm_timeout_s: float = 20.0

    # Retries are bounded by the turn budget, not just by this count: the
    # runner refuses a sleep that would eat the time the retry needs to
    # succeed in. Two is chosen because a provider 429 usually clears on the
    # first retry and a third attempt mostly spends the customer's patience.
    llm_max_retries: int = 2
    llm_retry_base_s: float = 0.5
    max_request_bytes: int = 32_768
    max_voice_session_s: float = 300.0

    rate_limit_turns_per_minute_per_session: int = 12
    rate_limit_turns_per_minute_per_ip: int = 30
    max_concurrent_turns: int = 8

    # ------------------------------------------------------------ sessions --

    session_ttl_s: int = 60 * 60 * 4
    allowed_origins: list[str] = Field(default_factory=list)

    # --------------------------------------------------------------- cost --
    # Rates are configuration, not provider-reported. Stored with the date they
    # were taken so the drawer can label the estimate honestly.

    cost_rates_effective: str = "2026-11-19"
    cost_per_1k_input_tokens_usd: float = 0.00040
    cost_per_1k_output_tokens_usd: float = 0.00160
    cost_per_1k_embedding_tokens_usd: float = 0.00002
    cost_realtime_per_minute_usd: float = 0.00

    # ---------------------------------------------------------------- demo --

    fixtures_dir: str | None = Field(
        default=None,
        description="Directory holding accounts.json and policies/. Auto-located in a checkout.",
    )

    demo_mode: bool = Field(
        default=True,
        description="Enables fixture reset and fault injection. Both are session-scoped.",
    )

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        if v.startswith("postgresql://"):
            # Silent sync-driver fallback would block the event loop under load.
            return v.replace("postgresql://", "postgresql+psycopg://", 1)
        return v

    @property
    def use_managed_identity(self) -> bool:
        return not self.azure_openai_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
