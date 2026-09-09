"""Application settings, loaded from environment variables."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class FaRecord:
    fa_id: str
    name: str
    email: str
    daily_capacity: int


class Settings(BaseSettings):
    """Runtime configuration, read from the environment and a local .env file.

    Environment variables are matched case-insensitively, so ``APP_ENV`` in the
    environment populates ``app_env`` here.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "staging", "production"] = "development"
    app_name: str = "AI Client Engagement"
    log_level: str = "INFO"

    cors_allow_origins: str = Field(
        default="http://localhost,http://localhost:8000,http://127.0.0.1:8000,http://127.0.0.1",
        validation_alias=AliasChoices("CORS_ALLOW_ORIGINS"),
    )

    @property
    def cors_allow_origins_list(self) -> list[str]:
        """`cors_allow_origins`, split into the list CORSMiddleware wants."""
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]

    database_url: str = "postgresql+psycopg://ace:ace@localhost:5432/ace"
    db_timezone: str = "Africa/Nairobi"
    db_safe_role: str = "ace_safe"
    db_restricted_role: str = "ace_restricted"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_async_pool_size: int | None = None
    db_async_max_overflow: int | None = None
    worker_threads: int = 40
    slow_request_ms: int = 1000

    @property
    def max_db_connections(self) -> int:
        return self.db_pool_size + self.db_max_overflow

    @property
    def async_pool_size(self) -> int:
        if self.db_async_pool_size is None:
            return self.db_pool_size
        return self.db_async_pool_size

    @property
    def async_max_overflow(self) -> int:
        if self.db_async_max_overflow is None:
            return self.db_max_overflow
        return self.db_async_max_overflow

    @property
    def max_async_db_connections(self) -> int:
        return self.async_pool_size + self.async_max_overflow

    @property
    def max_total_db_connections(self) -> int:
        return self.max_db_connections + self.max_async_db_connections

    @property
    def worker_thread_count(self) -> int:
        return max(self.worker_threads, self.max_db_connections)

    embedding_provider: str = "hashing"
    embedding_model: str = "dev-hashing"
    embedding_batch_size: int = 64
    rag_retrieval_k: int = 3
    rag_min_score: float = 0.1

    cytonn_api_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("CY_API_BASE_URL", "CYTONN_API_BASE_URL"),
    )
    cytonn_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("CY_API_KEY", "CYTONN_API_KEY"),
    )
    cytonn_active_clients_url: str = Field(
        default="",
        validation_alias=AliasChoices("CY_ACTIVE_CLIENTS_URL", "CYTONN_API_ACTIVE_URL"),
    )

    llm_provider: str = "anthropic"
    anthropic_api_key: str = Field(default="", validation_alias=AliasChoices("ANTHROPIC_API_KEY"))
    llm_model: str = "claude-haiku-4-5-20251001"
    llm_temperature: float | None = None
    llm_max_tokens: int = 4096

    ollama_base_url: str = "http://localhost:11434"
    ollama_timeout_seconds: float = 300.0

    llamacpp_base_url: str = "http://localhost:8080"
    llamacpp_timeout_seconds: float = 300.0

    judge_llm_provider: str = ""
    judge_llm_model: str = ""
    judge_llm_temperature: float | None = None
    judge_llm_max_tokens: int = 1024

    ai_briefing_enabled: bool = False
    briefing_llm_provider: str = ""
    briefing_llm_model: str = ""
    briefing_llm_temperature: float | None = None
    briefing_llm_max_tokens: int = 1024
    briefing_prewarm_limit: int = 200

    agent_llm_provider: str = ""
    agent_llm_model: str = ""
    agent_llm_temperature: float | None = None
    agent_llm_max_tokens: int = 2048
    agent_run_after_risk_detection: bool = False

    langfuse_base_url: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    complaints_source: str = "stub"
    fa_assignment_source: str = ""
    fa_roster: str = Field(default="", validation_alias=AliasChoices("ACE_FA_ROSTER", "fa_roster"))

    @property
    def fa_records(self) -> tuple[FaRecord, ...]:
        """`fa_roster`, parsed into records in the order given. Malformed
        entries (wrong field count, empty id, name, or email, unparseable
        capacity) and repeats of an id already seen are dropped rather than
        raising, so one typo does not take the whole roster down.
        """
        records: list[FaRecord] = []
        seen: set[str] = set()
        for entry in self.fa_roster.split(","):
            parts = [part.strip() for part in entry.split(":")]
            if len(parts) != 4:
                continue
            fa_id, name, email, raw_capacity = parts
            if not fa_id or not name or not email:
                continue
            try:
                capacity = int(raw_capacity)
            except ValueError:
                continue
            if capacity <= 0 or fa_id in seen:
                continue
            seen.add(fa_id)
            records.append(FaRecord(fa_id=fa_id, name=name, email=email, daily_capacity=capacity))
        return tuple(records)

    ai_outreach_jwt_secret: str = Field(
        default="", validation_alias=AliasChoices("AI_OUTREACH_JWT_SECRET")
    )

    campaign_cooldown_days: int = 7

    agent_new_client_days: int = 30
    agent_awaiting_call_days: int = 2
    agent_contact_cooldown_days: int = 7

    # What one investigation of a group may spend on its own queries.
    agent_query_call_budget: int = 12
    agent_query_timeout_ms: int = 5000
    agent_query_max_conditions: int = 10
    agent_query_max_values: int = 25
    agent_query_max_periods: int = 12
    # A group smaller than this is not reported at all: with a narrow enough
    # filter, a count of one is a person.
    agent_query_min_group_size: int = 5

    # How many findings one agent run may write, so a screen cannot be flooded.
    agent_insight_write_cap: int = 8

    # What one group's investigation may spend, and how many groups a run
    # may investigate at the same time, so neither the model provider nor
    # the database is hit harder than it can take.
    agent_investigation_max_turns: int = 8
    agent_investigation_concurrency: int = 3

    tier_sampling_enabled: bool = True

    cohort_sample_cap: int = 25

    require_deliverable_contact: bool = False

    smtp_host: str = ""
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False
    smtp_timeout_seconds: float = 30.0
    email_sender: str = ""

    console_base_url: str = Field(
        default="", validation_alias=AliasChoices("CONSOLE_BASE_URL", "console_base_url")
    )

    admin_username: str = Field(default="", validation_alias=AliasChoices("ADMIN_USERNAME"))
    admin_password: str = Field(default="", validation_alias=AliasChoices("ADMIN_PASSWORD"))
    admin_secret_key: str = Field(
        default="dev-only-admin-secret", validation_alias=AliasChoices("ADMIN_SECRET_KEY")
    )

    console_session_secret_key: str = Field(
        default="dev-only-console-secret",
        validation_alias=AliasChoices("CONSOLE_SESSION_SECRET_KEY"),
    )

    @property
    def is_production(self) -> bool:
        """True when running in the production environment."""
        return self.app_env == "production"

    @property
    def langfuse_enabled(self) -> bool:
        """True only once a host and both keys are configured."""
        return bool(
            self.langfuse_base_url and self.langfuse_public_key and self.langfuse_secret_key
        )


@lru_cache
def get_settings() -> Settings:
    """Return the application settings, cached for the process lifetime."""
    return Settings()
