"""Unit test (no I/O) for `MongoClientHolder.ping`'s failure path.

`_client` is monkeypatched to a stub whose `admin.command` raises, rather
than exercising this against a real (or deliberately unreachable) Mongo —
the point here is purely that a failure is logged with its cause and
counted, not that PyMongo actually raises `PyMongoError` in some
scenario (that's PyMongo's own contract, not this codebase's).
"""

from __future__ import annotations

from typing import Any

import pytest
from pymongo.errors import PyMongoError

from app.config import Settings
from app.infrastructure.mongodb.client import MongoClientHolder
from app.observability.metrics import mongo_ping_failures_total


class _FailingAdmin:
    async def command(self, *args: Any, **kwargs: Any) -> None:
        raise PyMongoError("server selection timed out")


class _StubClient:
    def __init__(self) -> None:
        self.admin = _FailingAdmin()


async def test_ping_failure_is_logged_with_its_cause_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder = MongoClientHolder(Settings(_env_file=None))
    holder._client = _StubClient()  # ty: ignore[invalid-assignment]

    logged: dict[str, Any] = {}
    monkeypatch.setattr(
        "app.infrastructure.mongodb.client.logger.warning",
        lambda event, **kwargs: logged.update(event=event, **kwargs),
    )
    before = mongo_ping_failures_total._value.get()

    assert await holder.ping() is False

    assert logged["event"] == "mongo.ping_failed"
    assert logged["error"] == "server selection timed out"
    assert logged["exc_info"] is not None
    assert mongo_ping_failures_total._value.get() == before + 1
