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


# --- CRUD ---


async def test_create_policy_returns_201_with_server_assigned_fields(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, _policy_repo, _server_repo = app_context
    payload = {
        "name": "create-test-policy",
        "category": "connectivity",
        "severity": "WARNING",
        "condition": {"metric": "connectivity.fabric_paths_down", "operator": "GTE", "value": 1},
        "evidence": [{"key": "down", "metric": "connectivity.fabric_paths_down"}],
        "message_template": "{down} down",
        "source": "GLOBAL_CUSTOM",
        "priority": 200,
    }

    resp = await client.post("/api/v1/health-policies", json=payload)

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "create-test-policy"
    assert body["system"] is False
    assert body["revision"] == 1
    assert body["id"].startswith("hpol_")
    # policy_key defaults to the new policy's own id when not supplied.
    assert body["policy_key"] == body["id"]
    assert body["stats"] == {
        "fire_count": 0,
        "last_fired_at": None,
        "error_count": 0,
        "quarantined": False,
    }


async def test_get_policy_returns_created_policy(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, policy_repo, _server_repo = app_context
    policy = _make_policy("get-test-policy")
    await policy_repo.upsert(policy)

    resp = await client.get(f"/api/v1/health-policies/{policy.id}")

    assert resp.status_code == 200
    assert resp.json()["name"] == "get-test-policy"


async def test_get_policy_404_for_missing(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, _policy_repo, _server_repo = app_context

    resp = await client.get("/api/v1/health-policies/hpol_does_not_exist")

    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_list_policies_filters_by_enabled(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, policy_repo, _server_repo = app_context
    await policy_repo.upsert(_make_policy("enabled-one", enabled=True))
    await policy_repo.upsert(_make_policy("disabled-one", enabled=False))

    resp = await client.get("/api/v1/health-policies", params={"enabled": "true"})

    assert resp.status_code == 200
    names = {item["name"] for item in resp.json()["items"]}
    assert names == {"enabled-one"}


async def test_update_policy_bumps_revision_and_changes_field(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, policy_repo, _server_repo = app_context
    policy = _make_policy("update-test-policy")
    await policy_repo.upsert(policy)

    resp = await client.patch(f"/api/v1/health-policies/{policy.id}", json={"priority": 250})

    assert resp.status_code == 200
    body = resp.json()
    assert body["priority"] == 250
    assert body["revision"] == 2


async def test_delete_policy_returns_204_then_404_on_get(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, policy_repo, _server_repo = app_context
    policy = _make_policy("delete-test-policy")
    await policy_repo.upsert(policy)

    delete_resp = await client.delete(f"/api/v1/health-policies/{policy.id}")
    assert delete_resp.status_code == 204

    get_resp = await client.get(f"/api/v1/health-policies/{policy.id}")
    assert get_resp.status_code == 404


async def test_create_duplicate_name_returns_409(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, policy_repo, _server_repo = app_context
    await policy_repo.upsert(_make_policy("dup-name-policy"))

    payload = {
        "name": "dup-name-policy",
        "category": "connectivity",
        "severity": "WARNING",
        "condition": {"metric": "connectivity.fabric_paths_down", "operator": "GTE", "value": 1},
        "message_template": "down",
        "source": "GLOBAL_CUSTOM",
        "priority": 200,
    }
    resp = await client.post("/api/v1/health-policies", json=payload)

    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFLICT"


# --- Validation errors ---


async def test_create_unknown_metric_returns_422(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, _policy_repo, _server_repo = app_context
    payload = {
        "name": "bad-metric-policy",
        "category": "connectivity",
        "severity": "WARNING",
        "condition": {"metric": "not.a.real.metric", "operator": "GTE", "value": 1},
        "message_template": "down",
        "source": "GLOBAL_CUSTOM",
        "priority": 200,
    }

    resp = await client.post("/api/v1/health-policies", json=payload)

    assert resp.status_code == 422
    assert resp.json()["code"] == "UNKNOWN_METRIC"


async def test_create_bad_template_returns_422(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, _policy_repo, _server_repo = app_context
    payload = {
        "name": "bad-template-policy",
        "category": "connectivity",
        "severity": "WARNING",
        "condition": {"metric": "connectivity.fabric_paths_down", "operator": "GTE", "value": 1},
        "evidence": [],
        "message_template": "{undeclared_field} down",
        "source": "GLOBAL_CUSTOM",
        "priority": 200,
    }

    resp = await client.post("/api/v1/health-policies", json=payload)

    assert resp.status_code == 422
    assert resp.json()["code"] == "TEMPLATE_INVALID"


async def test_create_priority_out_of_band_returns_422(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, _policy_repo, _server_repo = app_context
    payload = {
        "name": "bad-priority-policy",
        "category": "connectivity",
        "severity": "WARNING",
        "condition": {"metric": "connectivity.fabric_paths_down", "operator": "GTE", "value": 1},
        "message_template": "down",
        "source": "GLOBAL_CUSTOM",
        "priority": 999,  # GLOBAL_CUSTOM band is 200-299
    }

    resp = await client.post("/api/v1/health-policies", json=payload)

    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_create_scope_source_mismatch_returns_422(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, _policy_repo, _server_repo = app_context
    payload = {
        "name": "bad-scope-policy",
        "category": "connectivity",
        "severity": "WARNING",
        "condition": {"metric": "connectivity.fabric_paths_down", "operator": "GTE", "value": 1},
        "message_template": "down",
        "source": "SITE_CUSTOM",  # requires scope.site_id
        "priority": 500,
    }

    resp = await client.post("/api/v1/health-policies", json=payload)

    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


# --- System policy protections ---


async def test_delete_system_policy_returns_422(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, policy_repo, _server_repo = app_context
    system_policy = _make_policy(
        "a-system-policy", source="SYSTEM_DEFAULT", priority=100, system=True
    )
    await policy_repo.upsert(system_policy)

    resp = await client.delete(f"/api/v1/health-policies/{system_policy.id}")

    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_update_system_policy_non_enabled_field_returns_422(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, policy_repo, _server_repo = app_context
    system_policy = _make_policy(
        "another-system-policy", source="SYSTEM_DEFAULT", priority=100, system=True
    )
    await policy_repo.upsert(system_policy)

    resp = await client.patch(f"/api/v1/health-policies/{system_policy.id}", json={"priority": 150})

    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_update_system_policy_enabled_field_succeeds(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, policy_repo, _server_repo = app_context
    system_policy = _make_policy(
        "toggle-system-policy",
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        enabled=True,
    )
    await policy_repo.upsert(system_policy)

    resp = await client.patch(
        f"/api/v1/health-policies/{system_policy.id}", json={"enabled": False}
    )

    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


# --- Preview ---


async def test_preview_matches_expected_servers_with_known_facts(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, _policy_repo, server_repo = app_context
    await server_repo.upsert(_make_server("srv-zero-down", index=1, fabric_paths_down=0))
    await server_repo.upsert(_make_server("srv-one-down", index=2, fabric_paths_down=1))
    await server_repo.upsert(_make_server("srv-two-down", index=3, fabric_paths_down=2))

    # Draft: exactly-one-path-down warning (spec §71 acceptance scenario).
    payload = {
        "name": "preview-warning-draft",
        "category": "connectivity",
        "severity": "WARNING",
        "condition": {"metric": "connectivity.fabric_paths_down", "operator": "EQ", "value": 1},
        "evidence": [{"key": "down", "metric": "connectivity.fabric_paths_down"}],
        "message_template": "{down} path down",
        "source": "GLOBAL_CUSTOM",
        "priority": 200,
    }

    resp = await client.post("/api/v1/health-policies/preview", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["matched_count"] == 1
    assert body["truncated"] is False
    assert body["mode"] == "sampled"
    assert len(body["sample"]) == 1
    assert body["sample"][0]["name"] == "srv-one-down"
    assert body["sample"][0]["would_be_severity"] == "WARNING"


async def test_preview_reflects_shadowing_site_override_wins(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    """The core mechanism a preview must respect: a lower-precedence
    override still WINS its family over a higher-severity global default
    when it's more specific (site-scoped beats unscoped) — so previewing
    the override must report the override's own severity, not the
    default's, for a server in that site.
    """
    client, policy_repo, server_repo = app_context
    shared_key = "connectivity.fabric_paths_down_shadow_test"
    global_critical = _make_policy(
        "global-critical-default",
        policy_key=shared_key,
        source="GLOBAL_CUSTOM",
        priority=200,
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=2),
    )
    await policy_repo.upsert(global_critical)

    await server_repo.upsert(
        _make_server("srv-in-shadow-site", index=1, site_id="site_shadow", fabric_paths_down=2)
    )

    # Draft: a SITE_CUSTOM WARNING override for site_shadow, same
    # policy_key -> same family as the saved global default.
    draft_payload = {
        "name": "site-warning-override-draft",
        "policy_key": shared_key,
        "category": "connectivity",
        "severity": "WARNING",
        "condition": {"metric": "connectivity.fabric_paths_down", "operator": "GTE", "value": 2},
        "evidence": [{"key": "down", "metric": "connectivity.fabric_paths_down"}],
        "message_template": "{down} down (site override)",
        "scope": {"site_id": "site_shadow"},
        "source": "SITE_CUSTOM",
        "priority": 500,
    }

    resp = await client.post("/api/v1/health-policies/preview", json=draft_payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["matched_count"] == 1
    assert body["sample"][0]["name"] == "srv-in-shadow-site"
    assert body["sample"][0]["would_be_severity"] == "WARNING"


async def test_preview_draft_shadowed_by_existing_higher_specificity_policy_does_not_match(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    """The inverse of the override scenario: a lower-specificity DRAFT
    with a matching condition must NOT be reported as a match for a
    server where an existing, more-specific, already-saved policy in the
    same family wins instead. Proves preview checks "does the draft's OWN
    evaluation win", not "does the draft's condition match in isolation".
    """
    client, policy_repo, server_repo = app_context
    shared_key = "connectivity.fabric_paths_down_inverse_shadow_test"
    existing_site_critical = _make_policy(
        "existing-site-critical",
        policy_key=shared_key,
        source="SITE_CUSTOM",
        priority=500,
        severity=HealthSeverity.CRITICAL,
        scope=PolicyScope(site_id="site_shadow_inverse"),
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=1),
    )
    await policy_repo.upsert(existing_site_critical)

    await server_repo.upsert(
        _make_server(
            "srv-in-inverse-site", index=1, site_id="site_shadow_inverse", fabric_paths_down=1
        )
    )
    await server_repo.upsert(
        _make_server("srv-elsewhere", index=2, site_id=None, fabric_paths_down=1)
    )

    # Draft: an unscoped GLOBAL_CUSTOM warning, same policy_key, condition
    # also matches -- but is lower-specificity than the saved site policy.
    draft_payload = {
        "name": "global-warning-draft",
        "policy_key": shared_key,
        "category": "connectivity",
        "severity": "WARNING",
        "condition": {"metric": "connectivity.fabric_paths_down", "operator": "GTE", "value": 1},
        "evidence": [{"key": "down", "metric": "connectivity.fabric_paths_down"}],
        "message_template": "{down} down (global)",
        "source": "GLOBAL_CUSTOM",
        "priority": 200,
    }

    resp = await client.post("/api/v1/health-policies/preview", json=draft_payload)

    assert resp.status_code == 200
    body = resp.json()
    # Only the unscoped server matches -- the scoped server's family is
    # won by the existing, more-specific site policy, not the draft.
    assert body["matched_count"] == 1
    assert body["sample"][0]["name"] == "srv-elsewhere"


async def test_preview_unknown_metric_returns_422(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, _policy_repo, _server_repo = app_context

    payload = {
        "name": "preview-bad-metric-draft",
        "category": "connectivity",
        "severity": "WARNING",
        "condition": {"metric": "not.a.real.metric", "operator": "GTE", "value": 1},
        "message_template": "down",
        "source": "GLOBAL_CUSTOM",
        "priority": 200,
    }

    resp = await client.post("/api/v1/health-policies/preview", json=payload)

    assert resp.status_code == 422
    assert resp.json()["code"] == "UNKNOWN_METRIC"


# --- Health metrics registry ---


async def test_list_health_metrics_includes_core_metrics(
    app_context: tuple[AsyncClient, MongoHealthPolicyRepository, MongoServerRepository],
) -> None:
    client, _policy_repo, _server_repo = app_context

    resp = await client.get("/api/v1/health-metrics")

    assert resp.status_code == 200
    names = {item["name"] for item in resp.json()["items"]}
    assert "connectivity.fabric_paths_down" in names
    assert "storage.failed_drive_count" in names
