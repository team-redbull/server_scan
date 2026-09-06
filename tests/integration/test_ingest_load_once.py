"""P1 regression (`docs/notes/2026-09-audit.md`): a run loads its ruleset
and policy set once, not once per server.

Before this fix, `IngestService._ingest_one` called `ClassificationService
.classify_server`/`HealthPolicyService.evaluate_server` per server, each
of which does its own uncached `list_all()` — a 10,000-server run issued
~20,000 collection reads for an answer that cannot change while the run
is in progress. `ingest()` now calls `load_ruleset`/`load_policies` once
per run and passes the result to every server via `classify_with_ruleset`/
`evaluate_with_policies`.

Asserted here by counting real calls to both repositories' `list_all`
during a multi-server run against the live dev MongoDB — not a fake
double, so the count is of the actual query this fix removes.
"""

from __future__ import annotations

import pytest

from app.application.services.classification_service import ClassificationService
from app.application.services.health_policy_service import HealthPolicyService
from app.application.services.ingest import IngestService
from app.domain.models.classification_rule import ClassificationRule
from app.domain.models.health_policy import HealthPolicy
from app.domain.services.health.metrics import build_default_registry
from app.domain.services.regex_engine import RegexModuleEngine
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository
from app.infrastructure.providers.fake.generator import list_managers, list_sites
from app.infrastructure.providers.fake.provider import fake_providers

SITES = site_catalog("")
ENGINE = RegexModuleEngine(max_pattern_length=200, match_timeout_seconds=0.25)
REGISTRY = build_default_registry()

pytestmark = pytest.mark.integration

_CURSOR_SECRET = "test-cursor-secret"


class _CountingRuleRepo(MongoClassificationRuleRepository):
    def __init__(self, mongo: MongoClientHolder) -> None:
        super().__init__(mongo)
        self.list_all_calls = 0

    async def list_all(self, *, enabled_only: bool = False) -> list[ClassificationRule]:
        self.list_all_calls += 1
        return await super().list_all(enabled_only=enabled_only)


class _CountingPolicyRepo(MongoHealthPolicyRepository):
    def __init__(self, mongo: MongoClientHolder) -> None:
        super().__init__(mongo)
        self.list_all_calls = 0

    async def list_all(self, *, enabled_only: bool = False) -> list[HealthPolicy]:
        self.list_all_calls += 1
        return await super().list_all(enabled_only=enabled_only)


async def test_ruleset_and_policies_are_loaded_once_per_run_not_once_per_server(
    mongo_holder: MongoClientHolder,
) -> None:
    rule_repo = _CountingRuleRepo(mongo_holder)
    policy_repo = _CountingPolicyRepo(mongo_holder)
    service = IngestService(
        sites=SITES,
        server_repo=MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET),
        site_repo=MongoSiteRepository(mongo_holder),
        manager_repo=MongoManagerRepository(mongo_holder),
        classification_service=ClassificationService(rule_repo=rule_repo, engine=ENGINE),
        health_service=HealthPolicyService(policy_repo=policy_repo, registry=REGISTRY),
    )

    fleet_reads = 0
    for provider in fake_providers(seed=11, count=25):
        fleet_reads += 1
        summary = await service.ingest(provider, sites=list_sites(), managers=list_managers())
        assert summary.errors == 0

    # One `list_all` per collector's `ingest()` call, never per server —
    # each fake collector owns a slice of the 25-server fleet, and every
    # slice still costs exactly one read of each collection.
    assert rule_repo.list_all_calls == fleet_reads
    assert policy_repo.list_all_calls == fleet_reads
    assert rule_repo.list_all_calls < 25
    assert policy_repo.list_all_calls < 25
