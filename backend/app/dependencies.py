"""FastAPI dependency providers.

Resource *construction* (the Mongo/Redis clients) happens once in
`app.main`'s lifespan and is stashed on `app.state`; these dependency
functions only *retrieve* what's already there. This keeps route handlers
free of any global-singleton imports, which is what makes
`app.dependency_overrides` usable in tests without monkeypatching module
globals.
"""

from __future__ import annotations

from fastapi import Request

from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.redis import RedisClientHolder


def get_mongo_holder(request: Request) -> MongoClientHolder:
    holder: MongoClientHolder = request.app.state.mongo
    return holder


def get_redis_holder(request: Request) -> RedisClientHolder:
    holder: RedisClientHolder = request.app.state.redis
    return holder
