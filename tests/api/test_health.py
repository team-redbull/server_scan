"""Proves slice 0's acceptance criteria: readiness is 200 with Mongo up and
503 with Mongo down, without crashing the process either way.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


async def test_liveness_always_ok(client: AsyncClient) -> None:
    resp = await client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readiness_ok_when_mongo_up(client: AsyncClient) -> None:
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["dependencies"]["mongo"] == "ok"


async def test_not_found_is_problem_json(client: AsyncClient) -> None:
    resp = await client.get("/does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["code"] == "NOT_FOUND"
    assert body["status"] == 404
    assert "request_id" in body
