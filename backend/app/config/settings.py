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
    # Signs opaque keyset-pagination cursors (see `app.domain.services.cursor`)
    # so a client can never forge one that skips the filter/sort binding
    # check. Insecure default is fine for dev/test; production deployments
    # must override via INVENTORY_CURSOR_SECRET.
    cursor_secret: str = "dev-insecure-cursor-secret-change-in-production"  # noqa: S105 - dev default, not a real secret

    # --- Regex / classification safety ---
    regex_max_pattern_length: int = 200
    regex_match_timeout_seconds: float = 0.25

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Metrics ---
    metrics_enabled: bool = True

    # --- Collectors (tools/run_collector.py, not the API process) ---
    #
    # A credential pair per manager type, plus an endpoint for every type
    # that is pointed at directly — and that is the whole of a collector's
    # connection config. There is no per-manager document to maintain and
    # no secret directory to mount. Every value is empty by default so a
    # collector for an unconfigured vendor fails with an explicit "not
    # configured" error naming the variables to set, rather than
    # attempting a connection to nowhere.
    #
    # Cisco UCS Manager is the one type with a login but **no endpoint**.
    # It is never pointed at directly: the UCS Central collector reaches
    # every registered domain in turn, at the address Central reports for
    # each (`ComputeSystem.address`), using this one fleet-wide service
    # account. An `ucs_manager_ip` would have to name a single domain,
    # which would be either unused or actively misleading — see
    # `app.infrastructure.credentials.env`, where the split between
    # "has a login" and "has an endpoint" is a difference of type.
    #
    # `INVENTORY_`-prefixed, so `ucs_manager_ip` is
    # `INVENTORY_UCS_MANAGER_IP`. In Kubernetes these arrive from a
    # Secret via `envFrom` — see `deploy/helm/server-inventory/values.yaml`.
    #
    # Cisco Intersight keeps the same three fields for a uniform values
    # file, but they mean something different there: it signs requests
    # with an API key rather than logging in, so `username` carries the
    # API Key ID and `password` the secret key. `ip` is `intersight.com`
    # for the SaaS tenant, or the appliance FQDN for Connected Virtual
    # Appliance. Called out here and in values.yaml because handing
    # Intersight an account password would look plausible and never work.
    ucs_manager_username: str = ""
    ucs_manager_password: str = ""

    ucs_central_ip: str = ""
    ucs_central_username: str = ""
    ucs_central_password: str = ""

    oneview_ip: str = ""
    oneview_username: str = ""
    oneview_password: str = ""

    ome_ip: str = ""
    ome_username: str = ""
    ome_password: str = ""

    # username = API Key ID, password = secret key — see the note above.
    intersight_ip: str = ""
    intersight_username: str = ""
    intersight_password: str = ""

    collector_connect_timeout_seconds: float = 15.0

    # How many domains the UCS Central collector talks to at once. It uses
    # Central only to enumerate registered domains and their service-profile
    # names, then reads each domain's real inventory from that domain's own
    # UCS Manager — so it needs INVENTORY_UCS_MANAGER_USERNAME/_PASSWORD
    # (one login valid across the fleet; the addresses come from Central,
    # so INVENTORY_UCS_MANAGER_IP is not used by this collector).
    #
    # Domains are independent, and each costs a login plus ~9 queries no
    # matter how many servers it holds, so this bounds wall-clock without
    # bounding correctness. Kept modest: every concurrent domain is one
    # blocking SDK call parked in a worker thread.
    ucs_central_domain_concurrency: int = 4

    # How many Dell servers the OpenManage collector inventories at once.
    # One OME appliance answers the whole estate, so this bounds the
    # per-device inventory fan-out (each matched server costs one HTTP call
    # per hardware section) without bounding correctness — the two bulk
    # enumeration calls run once regardless. Kept modest to stay a polite
    # client of a single appliance.
    ome_inventory_concurrency: int = 8

    # Which servers a collector is allowed to ingest at all, as a regex
    # matched against the server's name (`re.search`, so "starts with" is
    # spelled `^ocp`). A vendor manager holds the whole datacenter, not
    # just this platform's fleet, and there is no other way to tell the
    # two apart — the name is the only thing that carries the
    # distinction, which is already true of site parsing and
    # classification.
    #
    # Empty means collect everything, because the alternative — a
    # built-in default pattern — silently drops servers for anyone whose
    # naming differs, with an empty inventory as the only symptom. Set it
    # explicitly per deployment; `.env.example` and `values.yaml` both
    # ship `^ocp`.
    #
    # This is a *collection* filter, not a classification one: a
    # non-matching server is never fetched into MongoDB, so it has no
    # document, no health state and no audit trail. Deciding UPI vs.
    # hosted *within* the collected fleet is the classification engine's
    # job (`app.domain.services.classification`), not this.
    collector_name_pattern: str = ""

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
