"""MongoDB client lifecycle.

One `AsyncMongoClient` per process, created during FastAPI's `lifespan` and
closed on shutdown — never per-request. `AsyncMongoClient` is PyMongo's own
native async driver (unifying PyMongo and the now-deprecated Motor library),
not Motor: Motor entered its deprecation window in May 2026, so any new
project should be built directly on it rather than on the library slated to
be phased out from under it.

Pool and timeout settings are explicit rather than left at driver defaults,
per the platform's engineering requirements: an air-gapped estate has no
"just retry against another region" escape hatch, so a hung connection must
fail fast and visibly instead of hanging a request indefinitely.
"""

from __future__ import annotations

from typing import Any

import structlog
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError

from app.config import Settings

logger = structlog.get_logger(__name__)

# Document type is left as `dict[str, Any]` at this layer: repositories
# (added in the inventory slice) are what convert raw documents to/from
# typed Pydantic domain models, so this generic parameter is deliberately
# untyped rather than bound to any one collection's shape.
_MongoClient = AsyncMongoClient[dict[str, Any]]
_MongoDatabase = AsyncDatabase[dict[str, Any]]


class MongoClientHolder:
    """Owns the single `AsyncMongoClient` for this process."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: _MongoClient | None = None

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = _MongoClient(
            self._settings.mongo_uri,
            connectTimeoutMS=self._settings.mongo_connect_timeout_ms,
            serverSelectionTimeoutMS=self._settings.mongo_server_selection_timeout_ms,
            socketTimeoutMS=self._settings.mongo_socket_timeout_ms,
            maxPoolSize=self._settings.mongo_max_pool_size,
            minPoolSize=self._settings.mongo_min_pool_size,
            appname=self._settings.service_name,
        )
        # Fail fast at startup rather than on the first request if Mongo is
        # unreachable or misconfigured.
        await self._client.admin.command("ping")
        logger.info("mongo.connected", database=self._settings.mongo_db)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            logger.info("mongo.closed")

    @property
    def db(self) -> _MongoDatabase:
        if self._client is None:
            raise RuntimeError("MongoClientHolder.connect() was not called")
        return self._client[self._settings.mongo_db]

    async def ping(self) -> bool:
        """Used by the readiness probe. Never raises — returns False on any
        failure so `/health/ready` can report 503 without itself crashing.
        """
        if self._client is None:
            return False
        try:
            await self._client.admin.command("ping")
            return True
        except PyMongoError:
            logger.warning("mongo.ping_failed")
            return False
