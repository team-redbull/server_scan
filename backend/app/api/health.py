"""Platform health endpoints.

Deliberately unversioned (mounted at `/health/live` and `/health/ready`, not
under `/api/v1`) — these are consumed by infrastructure (container
orchestrator probes, load balancers), not API clients, and must not move if
the API version ever changes.

`/health/live`: process liveness only. No dependency checks — if this
handler can run at all, the process is alive. An orchestrator uses this to
decide whether to restart the container.

`/health/ready`: dependency readiness. MongoDB must be reachable (source of
truth; the service cannot do useful work without it). Redis is reported but
never fails readiness — it is an ephemeral cache and the service is
explicitly designed to degrade to MongoDB when it's unavailable, so treating
a Redis outage as "not ready" would contradict that design.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.dependencies import get_mongo_holder, get_redis_holder
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.redis import RedisClientHolder

router = APIRouter(tags=["platform"])


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(
    response: Response,
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
    redis: Annotated[RedisClientHolder, Depends(get_redis_holder)],
) -> dict[str, object]:
    mongo_ok = await mongo.ping()
    redis_ok = await redis.ping()

    if not mongo_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if mongo_ok else "not_ready",
        "dependencies": {
            "mongo": "ok" if mongo_ok else "unreachable",
            "redis": "ok" if redis_ok else "degraded",
        },
    }
