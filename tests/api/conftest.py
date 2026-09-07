"""Skip `tests/api/` cleanly instead of erroring when the dev stack (Mongo
+ Redis, `scripts/dev-up.sh`) is down.

Every file in this directory builds its own `app_context` fixture that
calls `app.router.lifespan_context(app)`, and the lifespan's own
`mongo.connect()` raises straight out of that context manager on a dead
Mongo — an error, not a skip. `tests/integration/conftest.py` solved the
same problem for its own directory; this reuses the same memoized
reachability check (`tests/_stack_availability`) rather than restating it,
so `tests/api/` degrades to a skip list too. Measured before this existed:
with Mongo on a dead port, `tests/integration/` gave 5 clean skips while
`tests/api/` gave 3 errors in 21.23s — the exact "hung suite" symptom
`tests/integration/conftest.py`'s own docstring records as fixed, just not
here.

An autouse fixture rather than folding the check into each file's own
`app_context` — this directory has eight near-identical copies of that
fixture (its own known duplication, S4 in `docs/notes/
2026-09-audit-tools-tests.md`, not this fix's scope), and an autouse
fixture skips before any of them run without touching a single one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from tests._stack_availability import connect_mongo_or_skip, connect_redis_or_skip

from app.config import get_settings


@pytest.fixture(autouse=True)
async def _require_dev_stack() -> AsyncIterator[None]:
    settings = get_settings()
    mongo = await connect_mongo_or_skip(settings)
    await mongo.close()
    redis = await connect_redis_or_skip(settings)
    await redis.close()
    yield
