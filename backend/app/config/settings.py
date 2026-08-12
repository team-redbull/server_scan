"""Centralized application configuration.

All environment lookups go through this module — never scatter `os.environ`
calls through the codebase. Settings are loaded once at startup and injected
via FastAPI dependencies (see `app.main`), not imported as a bare module-level
singleton, so tests can override them cleanly with `dependency_overrides`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, sourced from environment variables (prefix INVENTORY_)
    and an optional `.env` file. See `.env.example` for the full list with
    explanations.
    """

    model_config = SettingsConfigDict(
        env_prefix="INVENTORY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Service identity ---
    service_name: str = "server-inventory-api"
    environment: Literal["development", "test", "staging", "production"] = "development"

    # --- HTTP server ---
    # Binding all interfaces is correct here, not a hardening gap: this is
    # the address the process listens on *inside* its own container, where
    # 127.0.0.1 would make it unreachable from the pod network entirely.
    # The actual exposure boundary is the container/Service network policy,
    # not this value.
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8080
    # NoDecode: without it, pydantic-settings tries to JSON-decode any
    # list-typed env var before our validator runs, so a plain comma-
    # separated value like "http://a,http://b" fails with a JSONDecodeError
    # instead of reaching `_split_csv` below.
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # --- MongoDB ---
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "server_inventory"
    mongo_connect_timeout_ms: int = 5_000
    mongo_server_selection_timeout_ms: int = 5_000
    mongo_socket_timeout_ms: int = 10_000
    mongo_max_pool_size: int = 100
    mongo_min_pool_size: int = 0

    # --- Redis ---
    redis_uri: str = "redis://localhost:6379/0"
    redis_connect_timeout_seconds: float = 2.0
    redis_socket_timeout_seconds: float = 2.0
    redis_max_connections: int = 50
    cache_default_ttl_seconds: int = 300

    # --- Pagination / search limits ---
    default_page_size: int = 50
    max_page_size: int = 200

    # --- Regex / classification safety ---
    regex_max_pattern_length: int = 200
    regex_match_timeout_seconds: float = 0.25

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Metrics ---
    metrics_enabled: bool = True

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton. Tests can call `get_settings.cache_clear()`
    after `monkeypatch.setenv(...)` to pick up overrides.
    """
    return Settings()
