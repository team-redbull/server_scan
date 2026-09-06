"""FastAPI dependency providers.

Resource *construction* (the Mongo/Redis clients) happens once in
`app.main`'s lifespan and is stashed on `app.state`; these dependency
functions only *retrieve* what's already there. This keeps route handlers
free of any global-singleton imports, which is what makes
`app.dependency_overrides` usable in tests without monkeypatching module
globals.

Every provider here is `async def`, not `def`, even though none of them
await anything — a sync dependency is still dispatched through Starlette's
`run_in_threadpool`, and an isolated A/B measured that handoff at
+0.14 ms per dependency (`docs/notes/2026-09-research-performance.md`
§7.4). `list_servers` alone pulls in three of these, so this was the
largest single measured cost in the read path with a one-keyword fix.
"""

from __future__ import annotations

from fastapi import Request

from app.domain.models.audit_event import Actor, ActorType
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.redis import RedisClientHolder


async def get_mongo_holder(request: Request) -> MongoClientHolder:
    holder: MongoClientHolder = request.app.state.mongo
    return holder


async def get_redis_holder(request: Request) -> RedisClientHolder:
    holder: RedisClientHolder = request.app.state.redis
    return holder


async def get_request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


# Placeholder until real authentication lands (the platform's own release
# gate — see the session's approved plan): every audit event recorded from
# an API request needs *some* actor, and until there's a `Principal` to
# extract one from, every request is attributed to this well-known
# unauthenticated actor rather than left null. `data.get("actor_id")` will
# stop returning this constant the moment auth is wired in — nothing about
# the audit event *shape* changes, only what this dependency returns.
_UNAUTHENTICATED_ACTOR = Actor(type=ActorType.USER, id="unauthenticated", display="API (no auth)")


async def get_current_actor(_request: Request) -> Actor:
    return _UNAUTHENTICATED_ACTOR
