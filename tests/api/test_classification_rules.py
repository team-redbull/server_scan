"""API tests for `/api/v1/classification-rules`, against a real running
app (lifespan included) and the live dev Mongo stack. Test data (both
rules and servers used for `preview`) is inserted directly via the Mongo
repositories — never through the fake generator — to keep these tests
focused on the HTTP/validation contract, not ingestion. Same
`httpx.AsyncClient` + `ASGITransport` + lifespan pattern as
`tests/api/test_servers.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.domain.enums import InstallationType, Vendor
from app.domain.models.classification import Classification
from app.domain.models.classification_rule import ClassificationRule, RuleScope
from app.domain.models.server import Identity, Server
from app.domain.services.normalize import normalize_text
from app.domain.services.search_tokens import build_search_tokens
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.main import create_app
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

_CURSOR_SECRET = "test-cursor-secret"


def _make_server(
    name: str, *, vendor: Vendor = Vendor.DELL, index: int = 0, site_id: str | None = None
) -> Server:
    now = utcnow()
    serial = f"CRAPITEST{index:06d}"
    server = Server(
        _id=new_id("server"),
        name=name,
        name_normalized=normalize_text(name),
        identity=Identity(
            vendor=vendor,
            serial=serial,
            serial_normalized=normalize_text(serial),
            system_uuid=f"crapi-test-uuid-{index:06d}",
        ),
        site_id=site_id,
        classification=Classification(installation_type=InstallationType.UNCLASSIFIED),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    server.search_tokens = build_search_tokens(server)
    return server


def _make_rule(
    name: str,
    *,
    priority: int = 200,
    source: str = "GLOBAL_CUSTOM",
    scope: RuleScope | None = None,
    system: bool = False,
    enabled: bool = True,
    pattern: str = r"^ocp-.*",
) -> ClassificationRule:
    now = utcnow()
    return ClassificationRule(
        _id=new_id("classification_rule"),
        name=name,
        installation_type=InstallationType.HOSTED_CLUSTER,
        scope=scope or RuleScope(),
        field="name",
        pattern=pattern,
        source=source,
        priority=priority,
        system=system,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
async def app_context() -> AsyncIterator[
    tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository]
]:
    settings = get_settings()
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        mongo: MongoClientHolder = app.state.mongo
        for name in ("servers", "sites", "managers", "classification_rules"):
            await mongo.db[name].delete_many({})

        rule_repo = MongoClassificationRuleRepository(mongo)
        server_repo = MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)
        yield client, rule_repo, server_repo

        for name in ("servers", "sites", "managers", "classification_rules"):
            await mongo.db[name].delete_many({})


# --- CRUD ---


async def test_create_rule_returns_201_with_server_assigned_fields(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, _rule_repo, _server_repo = app_context
    payload = {
        "name": "create-test-rule",
        "installation_type": "HOSTED_CLUSTER",
        "field": "name",
        "pattern": "^ocp-.*",
        "source": "GLOBAL_CUSTOM",
        "priority": 200,
    }

    resp = await client.post("/api/v1/classification-rules", json=payload)

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "create-test-rule"
    assert body["system"] is False
    assert body["revision"] == 1
    assert body["id"].startswith("crul_")
    assert body["stats"] == {
        "match_count": 0,
        "last_matched_at": None,
        "timeout_count": 0,
        "quarantined": False,
    }


async def test_get_rule_returns_created_rule(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, rule_repo, _server_repo = app_context
    rule = _make_rule("get-test-rule")
    await rule_repo.upsert(rule)

    resp = await client.get(f"/api/v1/classification-rules/{rule.id}")

    assert resp.status_code == 200
    assert resp.json()["name"] == "get-test-rule"


async def test_get_rule_404_for_missing(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, _rule_repo, _server_repo = app_context

    resp = await client.get("/api/v1/classification-rules/crul_does_not_exist")

    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_list_rules_filters_by_enabled(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, rule_repo, _server_repo = app_context
    await rule_repo.upsert(_make_rule("enabled-one", enabled=True))
    await rule_repo.upsert(_make_rule("disabled-one", enabled=False))

    resp = await client.get("/api/v1/classification-rules", params={"enabled": "true"})

    assert resp.status_code == 200
    names = {item["name"] for item in resp.json()["items"]}
    assert names == {"enabled-one"}


async def test_update_rule_bumps_revision_and_changes_field(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, rule_repo, _server_repo = app_context
    rule = _make_rule("update-test-rule")
    await rule_repo.upsert(rule)

    resp = await client.patch(
        f"/api/v1/classification-rules/{rule.id}", json={"pattern": r"^ocp-updated-.*"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["pattern"] == r"^ocp-updated-.*"
    assert body["revision"] == 2


async def test_delete_rule_returns_204_then_404_on_get(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, rule_repo, _server_repo = app_context
    rule = _make_rule("delete-test-rule")
    await rule_repo.upsert(rule)

    delete_resp = await client.delete(f"/api/v1/classification-rules/{rule.id}")
    assert delete_resp.status_code == 204

    get_resp = await client.get(f"/api/v1/classification-rules/{rule.id}")
    assert get_resp.status_code == 404


async def test_create_duplicate_name_returns_409(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, rule_repo, _server_repo = app_context
    await rule_repo.upsert(_make_rule("dup-name-rule"))

    payload = {
        "name": "dup-name-rule",
        "installation_type": "HOSTED_CLUSTER",
        "field": "name",
        "pattern": "^ocp-.*",
        "source": "GLOBAL_CUSTOM",
        "priority": 200,
    }
    resp = await client.post("/api/v1/classification-rules", json=payload)

    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFLICT"


# --- Validation errors ---


async def test_create_priority_out_of_band_returns_422(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, _rule_repo, _server_repo = app_context
    payload = {
        "name": "bad-priority-rule",
        "installation_type": "HOSTED_CLUSTER",
        "field": "name",
        "pattern": "^ocp-.*",
        "source": "GLOBAL_CUSTOM",
        "priority": 999,  # GLOBAL_CUSTOM band is 200-299
    }

    resp = await client.post("/api/v1/classification-rules", json=payload)

    assert resp.status_code == 422
    assert resp.json()["code"] == "RULE_SCOPE_INVALID"


async def test_create_scope_source_mismatch_returns_422(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, _rule_repo, _server_repo = app_context
    payload = {
        "name": "bad-scope-rule",
        "installation_type": "HOSTED_CLUSTER",
        "field": "name",
        "pattern": "^ocp-.*",
        "source": "SITE_CUSTOM",  # requires scope.site_id
        "priority": 500,
    }

    resp = await client.post("/api/v1/classification-rules", json=payload)

    assert resp.status_code == 422
    assert resp.json()["code"] == "RULE_SCOPE_INVALID"


async def test_create_unsafe_regex_returns_422(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, _rule_repo, _server_repo = app_context
    payload = {
        "name": "bad-pattern-rule",
        "installation_type": "HOSTED_CLUSTER",
        "field": "name",
        "pattern": "(unclosed",
        "source": "GLOBAL_CUSTOM",
        "priority": 200,
    }

    resp = await client.post("/api/v1/classification-rules", json=payload)

    assert resp.status_code == 422
    assert resp.json()["code"] == "REGEX_UNSAFE"


# --- System rule protections ---


async def test_delete_system_rule_returns_422(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, rule_repo, _server_repo = app_context
    system_rule = _make_rule("a-system-rule", source="SYSTEM_DEFAULT", priority=100, system=True)
    await rule_repo.upsert(system_rule)

    resp = await client.delete(f"/api/v1/classification-rules/{system_rule.id}")

    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_update_system_rule_non_enabled_field_returns_422(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, rule_repo, _server_repo = app_context
    system_rule = _make_rule(
        "another-system-rule", source="SYSTEM_DEFAULT", priority=100, system=True
    )
    await rule_repo.upsert(system_rule)

    resp = await client.patch(
        f"/api/v1/classification-rules/{system_rule.id}", json={"pattern": r"^changed-.*"}
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_update_system_rule_enabled_field_succeeds(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, rule_repo, _server_repo = app_context
    system_rule = _make_rule(
        "toggle-system-rule", source="SYSTEM_DEFAULT", priority=100, system=True, enabled=True
    )
    await rule_repo.upsert(system_rule)

    resp = await client.patch(
        f"/api/v1/classification-rules/{system_rule.id}", json={"enabled": False}
    )

    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


# --- Preview ---


async def test_preview_matches_expected_servers_by_pattern(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, _rule_repo, server_repo = app_context
    await server_repo.upsert(_make_server("ocp-dell-worker-001", vendor=Vendor.DELL, index=1))
    await server_repo.upsert(_make_server("ocp-dell-worker-002", vendor=Vendor.DELL, index=2))
    await server_repo.upsert(_make_server("upi-dell-master-001", vendor=Vendor.DELL, index=3))
    await server_repo.upsert(_make_server("random-server-0004", vendor=Vendor.DELL, index=4))
    await server_repo.upsert(_make_server("ocp-cisco-worker-001", vendor=Vendor.CISCO, index=5))

    resp = await client.post(
        "/api/v1/classification-rules/preview",
        json={"field": "name", "pattern": "^ocp-dell-.*"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["matched_count"] == 2
    assert body["truncated"] is False
    assert body["mode"] == "sampled"
    matched_names = {item["name"] for item in body["sample"]}
    assert matched_names == {"ocp-dell-worker-001", "ocp-dell-worker-002"}


async def test_preview_narrows_by_vendor_scope(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, _rule_repo, server_repo = app_context
    await server_repo.upsert(_make_server("ocp-a-worker-001", vendor=Vendor.DELL, index=1))
    await server_repo.upsert(_make_server("ocp-b-worker-001", vendor=Vendor.CISCO, index=2))

    resp = await client.post(
        "/api/v1/classification-rules/preview",
        json={"field": "name", "pattern": "^ocp-.*", "scope": {"vendor": "dell"}},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["matched_count"] == 1
    assert body["sample"][0]["name"] == "ocp-a-worker-001"


async def test_preview_unsafe_pattern_returns_422(
    app_context: tuple[AsyncClient, MongoClassificationRuleRepository, MongoServerRepository],
) -> None:
    client, _rule_repo, _server_repo = app_context

    resp = await client.post(
        "/api/v1/classification-rules/preview",
        json={"field": "name", "pattern": "(unclosed"},
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "REGEX_UNSAFE"
