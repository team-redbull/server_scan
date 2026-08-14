"""Application entry point.

Startup order matters and is made explicit here rather than left to import
side effects: settings -> logging -> Mongo -> Redis -> ready. Each later
step can log through the structured logger because logging is configured
first; Mongo is connected before Redis because Mongo is the hard dependency
(startup fails if it's unreachable) while Redis is a soft one (startup
continues, readiness reports it as degraded).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.api.health import router as health_router
from app.api.v1.classification_rules import router as classification_rules_router
from app.api.v1.events import router as events_router
from app.api.v1.health_policies import router as health_policies_router
from app.api.v1.servers import router as servers_router
from app.api.v1.sites import router as sites_router
from app.application.services.bootstrap import (
    ensure_default_classification_rules,
    ensure_default_health_policies,
)
from app.config import get_settings
from app.exception_handlers import register_exception_handlers
from app.infrastructure.logging import configure_logging
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.indexes import ensure_indexes
from app.infrastructure.redis import RedisClientHolder
from app.middleware.request_context import RequestContextMiddleware
from app.observability.metrics import http_request_duration_seconds, http_requests_total

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service_name=settings.service_name,
        environment=settings.environment,
    )
    logger.info("app.starting", environment=settings.environment)

    mongo = MongoClientHolder(settings)
    await mongo.connect()
    await ensure_indexes(mongo.db)
    # Idempotent — see `ensure_default_*`'s docstring for why "seed only
    # if missing by name" is required here, not just convenient.
    await ensure_default_classification_rules(MongoClassificationRuleRepository(mongo))
    await ensure_default_health_policies(MongoHealthPolicyRepository(mongo))
    app.state.mongo = mongo

    redis = RedisClientHolder(settings)
    await redis.connect()
    app.state.redis = redis

    logger.info("app.ready")
    try:
        yield
    finally:
        logger.info("app.stopping")
        await redis.close()
        await mongo.close()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.service_name,
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(servers_router)
    app.include_router(classification_rules_router)
    app.include_router(health_policies_router)
    app.include_router(events_router)
    app.include_router(sites_router)

    if settings.metrics_enabled:

        @app.middleware("http")
        async def record_metrics(request: Request, call_next: RequestResponseEndpoint) -> Response:
            start = time.monotonic()
            response = await call_next(request)
            duration = time.monotonic() - start
            route = request.scope.get("route")
            path_label = route.path if route is not None else request.url.path
            http_requests_total.labels(
                method=request.method, path=path_label, status=response.status_code
            ).inc()
            http_request_duration_seconds.labels(method=request.method, path=path_label).observe(
                duration
            )
            return response

        @app.get("/metrics", include_in_schema=False)
        async def metrics() -> Response:
            return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
