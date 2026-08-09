"""Application settings, loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Application
    app_env: Literal["development", "staging", "production"] = "development"
    app_name: str = "AI Client Engagement"
    log_level: str = "INFO"

    # Database
    database_url: str = "postgresql+psycopg://ace:ace@localhost:5432/ace"
    # Time zone for database timestamps. Nairobi (EAT) is UTC+3, no daylight saving.
    db_timezone: str = "Africa/Nairobi"
    # Role the model-facing path switches into; it has no grant on pii_vault.
    db_safe_role: str = "ace_safe"
    # Role server-side re-attachment switches into; the only one with a grant
    # on pii_vault.
    db_restricted_role: str = "ace_restricted"

    # Embeddings for RAG. The same model must index and query, or similarity is
    # meaningless. The dev default is deterministic and needs no external service.
    embedding_provider: str = "hashing"
    embedding_model: str = "dev-hashing"
    embedding_batch_size: int = 64

    # How many report passages a draft is grounded on. Kept small on purpose:
    # every passage handed to the model is a passage it may quote a figure
    # from, so a long tail of weakly related market commentary is a licence to
    # cite something the email was never about.
    rag_retrieval_k: int = 3
    # Similarity below which a passage is dropped even if it is in the top k.
    # A section filter always returns its best few candidates, however unrelated
    # they are to the query, so rank alone is not evidence of relevance.
    rag_min_score: float = 0.1

    # Cy client data API. Reads CY_API_BASE_URL / CY_API_KEY
    cytonn_api_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("CY_API_BASE_URL", "CYTONN_API_BASE_URL"),
    )
    cytonn_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("CY_API_KEY", "CYTONN_API_KEY"),
    )

    # LLM provider for draft generation.
    llm_provider: str = "anthropic"
    anthropic_api_key: str = Field(default="", validation_alias=AliasChoices("ANTHROPIC_API_KEY"))
    llm_model: str = "claude-haiku-4-5-20251001"
    llm_temperature: float | None = None
    llm_max_tokens: int = 4096

    ollama_base_url: str = "http://localhost:11434"
    ollama_timeout_seconds: float = 300.0

    # LLM-as-judge model (llmops.judge). Empty provider/model falls back to
    # llm_provider/llm_model, so judging works with no extra config.
    judge_llm_provider: str = ""
    judge_llm_model: str = ""
    judge_llm_temperature: float | None = None
    judge_llm_max_tokens: int = 1024

    langfuse_base_url: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # Shared secret for the integration plane, a stopgap ahead of M8A.7's
    # scoped API keys / OAuth client-credentials. Empty means integration
    # endpoints refuse every request rather than run unprotected.
    integration_api_key: str = Field(
        default="", validation_alias=AliasChoices("INTEGRATION_API_KEY")
    )

    # Minimum days between two touches to the same client, across every
    # campaign, checked by the eligibility gate before each send.
    campaign_cooldown_days: int = 7

    # Off means every message needs review, regardless of tier. On means a
    # tier's own human_approval / review_sample_rate is honoured instead.
    tier_sampling_enabled: bool = False

    # On (the only safe setting once real contact data exists) means the
    # eligibility gate refuses to generate or send for a client with no
    # contact_email/contact_whatsapp on file. Off is a local-development-only
    # escape hatch, for exercising generation before /integration/contacts has
    # ever been called for a test client -- the parent system that will push
    # real contact data isn't live yet, so nothing can satisfy this gate in
    # dev without it.
    require_deliverable_contact: bool = True

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
