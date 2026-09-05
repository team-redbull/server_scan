"""API tests for `/api/v1/health-policies` and `/api/v1/health-metrics`,
against a real running app (lifespan included) and the live dev Mongo
stack. Test data (both policies and servers used for `preview`) is
inserted directly via the Mongo repositories — never through the fake
generator/ingest pipeline — to keep these tests focused on the
HTTP/validation/shadowing contract, not ingestion. Same
`httpx.AsyncClient` + `ASGITransport` + lifespan pattern as
`tests/api/test_servers.py` / `tests/api/test_classification_rules.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.domain.enums import HealthSeverity, Vendor
from app.domain.models.connectivity import Connectivity, ConnectivityFacts
from app.domain.models.health_policy import HealthPolicy, PolicyScope
from app.domain.models.server import Identity, Server
from app.domain.services.health.conditions import Condition
from app.domain.services.normalize import normalize_text
from app.domain.services.search_tokens import build_search_tokens
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.main import create_app
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

_CURSOR_SECRET = "test-cursor-secret"


def _make_server(
    name: str,
    *,
    vendor: Vendor = Vendor.CISCO,
    index: int = 0,
    site_id: str | None = None,
    fabric_paths_down: int = 0,
    fabric_paths_up: int = 2,
) -> Server:
    now = utcnow()
    serial = f"HPAPITEST{index:06d}"
    server = Server(
        _id=new_id("server"),
        name=name,
        name_normalized=normalize_text(name),
        identity=Identity(
            vendor=vendor,
            serial=serial,
            serial_normalized=normalize_text(serial),
            system_uuid=f"hpapi-test-uuid-{index:06d}",
        ),
        site_id=site_id,
        connectivity=Connectivity(
            facts=ConnectivityFacts(
                fabric_paths_total=fabric_paths_up + fabric_paths_down,
                fabric_paths_up=fabric_paths_up,
                fabric_paths_down=fabric_paths_down,
            )
        ),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    server.search_tokens = build_search_tokens(server)
    return server


def _make_policy(
    name: str,
    *,
    policy_key: str | None = None,
    priority: int = 200,
    source: str = "GLOBAL_CUSTOM",
    scope: PolicyScope | None = None,
    system: bool = False,
    enabled: bool = True,
    category: str = "connectivity",
    severity: HealthSeverity = HealthSeverity.WARNING,
    condition: Condition | None = None,
) -> HealthPolicy:
    now = utcnow()
    policy_id = new_id("health_policy")
    return HealthPolicy(
        id=policy_id,
        name=name,
        policy_key=policy_key or policy_id,
        category=category,
        severity=severity,
        condition=condition
        or Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=1),
        message_template="down",
        scope=scope or PolicyScope(),
        source=source,
        priority=priority,
        system=system,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
async def app_context() -> AsyncIterator[
    tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository]
]:
    settings = get_settings()
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        mongo: MongoClientHolder = app.state.mongo
        for name in ("servers", "sites", "managers", "health_policies"):
            await mongo.db[name].delete_many({})

        policy_repo = MongoHealthPolicyRepository(mongo)
        server_repo = MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)
        yield client, policy_repo, server_repo

        for name in ("servers", "sites", "managers", "health_policies"):
            await mongo.db[name].delete_many({})


# --- Reads ---


async def test_list_returns_every_policy(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    """The Rules page's second query."""
    client, policy_repo, _ = app_context
    await policy_repo.upsert(_make_policy("alpha"))
    await policy_repo.upsert(_make_policy("beta", enabled=False))

    resp = await client.get("/api/v1/health-policies")

    assert resp.status_code == 200
    assert {item["name"] for item in resp.json()["items"]} == {"alpha", "beta"}


async def test_list_exposes_the_condition_a_policy_evaluates(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    """A policy without its condition is a name and a severity with
    nothing connecting them — the operator equivalent of a rule without
    its pattern.
    """
    client, policy_repo, _ = app_context
    await policy_repo.upsert(
        _make_policy(
            "alpha",
            condition=Condition(metric="storage.failed_drive_count", operator="GTE", value=1),
        )
    )

    item = (await client.get("/api/v1/health-policies")).json()["items"][0]

    assert item["condition"]["metric"] == "storage.failed_drive_count"
    assert item["condition"]["operator"] == "GTE"
    assert item["condition"]["value"] == 1


async def test_list_filters_by_enabled(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, policy_repo, _ = app_context
    await policy_repo.upsert(_make_policy("on"))
    await policy_repo.upsert(_make_policy("off", enabled=False))

    enabled = (await client.get("/api/v1/health-policies?enabled=true")).json()
    disabled = (await client.get("/api/v1/health-policies?enabled=false")).json()

    assert [i["name"] for i in enabled["items"]] == ["on"]
    assert [i["name"] for i in disabled["items"]] == ["off"]


async def test_get_returns_one_policy(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, policy_repo, _ = app_context
    policy = await policy_repo.upsert(_make_policy("alpha"))

    resp = await client.get(f"/api/v1/health-policies/{policy.id}")

    assert resp.status_code == 200
    assert resp.json()["name"] == "alpha"


async def test_get_404_for_missing(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, _, _ = app_context

    assert (await client.get("/api/v1/health-policies/nope")).status_code == 404


async def test_health_metrics_are_still_listed(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    """`GET /health-metrics` describes what the engine can evaluate. It
    survived the write-endpoint removal because it is a read, and because
    it documents the vocabulary the shipped policies are written in.
    """
    client, _, _ = app_context

    resp = await client.get("/api/v1/health-metrics")

    assert resp.status_code == 200
    assert resp.json()["items"]


# --- The write surface is gone, and stays gone ---


async def test_no_endpoint_can_change_a_policy(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    """As for classification rules: leaving these reachable would make the
    read-only UI a convention rather than a guarantee. 405 rather than
    404, since the paths still serve `GET`.
    """
    client, policy_repo, _ = app_context
    policy = await policy_repo.upsert(_make_policy("alpha"))
    body = {
        "name": "new",
        "policy_key": "x",
        "category": "connectivity",
        "severity": "WARNING",
        "condition": {"metric": "connectivity.fabric_paths_down", "operator": "GTE", "value": 1},
        "message_template": "down",
    }

    responses = {
        "create": await client.post("/api/v1/health-policies", json=body),
        "update": await client.patch(
            f"/api/v1/health-policies/{policy.id}", json={"enabled": False}
        ),
        "delete": await client.delete(f"/api/v1/health-policies/{policy.id}"),
        "preview": await client.post("/api/v1/health-policies/preview", json=body),
    }

    for action, resp in responses.items():
        assert resp.status_code == 405, f"{action} answered {resp.status_code}, not 405"

    stored = await policy_repo.get_by_id(policy.id)
    assert stored is not None
    assert stored.enabled is True
