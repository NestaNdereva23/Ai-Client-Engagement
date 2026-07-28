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

    # Embeddings for RAG. The same model must index and query, or similarity is
    # meaningless. The dev default is deterministic and needs no external service.
    embedding_provider: str = "hashing"
    embedding_model: str = "dev-hashing"
    embedding_batch_size: int = 64

    # Cytonn client-data API. Reads CY_API_BASE_URL / CY_API_KEY
    cytonn_api_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("CY_API_BASE_URL", "CYTONN_API_BASE_URL"),
    )
    cytonn_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("CY_API_KEY", "CYTONN_API_KEY"),
    )

    # LLM provider for draft generation. Claude is primary; the provider name
    # picks the implementation, the rest configure it, so a future provider
    # slots in without code changes at call sites.
    llm_provider: str = "anthropic"
    anthropic_api_key: str = Field(default="", validation_alias=AliasChoices("ANTHROPIC_API_KEY"))
    # Defaults to the latest Claude model; override per environment without a
    # code change. Newer Claude models reject a non-default temperature, so
    # llm_temperature defaults to unset (omitted from the request) rather than
    # a fixed number.
    llm_model: str = "claude-opus-5"
    llm_temperature: float | None = None
    llm_max_tokens: int = 1024

    @property
    def is_production(self) -> bool:
        """True when running in the production environment."""
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Return the application settings, cached for the process lifetime."""
    return Settings()
