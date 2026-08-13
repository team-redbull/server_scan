"""The one integration seam for health-policy evaluation and preview.

`HealthPolicyService` ties together policy loading, fact extraction, and
the domain evaluation engine (`app.domain.services.health.evaluate.
evaluate_health`) so callers never have to remember to do those three
steps in the right order themselves. A later part of this session wires
`evaluate_server` into the ingestion pipeline — this module only exposes
it.

Deviation from the literal constructor signature in the task brief: the
brief's `HealthPolicyService.__init__` takes only `policy_repo` and
`registry`, but `preview()` — by its own spec — must scope-filter and
scan *existing servers*, which requires a server repository. There is no
way to implement the described preview algorithm without one, so the
constructor here also takes `server_repo`. Documented here and in the
final report rather than silently diverging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from app.domain.enums import HealthSeverity
from app.domain.models.health_policy import HealthPolicy
from app.domain.models.server import Server
from app.domain.services.health.conditions import ConditionValidationError, validate_condition
from app.domain.services.health.evaluate import HealthState, evaluate_health
from app.domain.services.health.facts import extract_facts
from app.domain.services.health.metrics import MetricRegistry
from app.domain.services.health.template import TemplateValidationError, validate_template
from app.errors import (
    ConditionInvalidError,
    MetricOperatorMismatchError,
    TemplateInvalidError,
    UnknownMetricError,
    ValidationAppError,
)
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

# Source -> which single scope field that source requires to be set (and,
# for GLOBAL_CUSTOM, requires *not* to be set). Mirrors the classification
# rule scope/source coherence rule described in the task brief; kept here
# rather than in `app.domain.models.health_policy` because it's a
# write-time business rule, not a structural invariant of the model
# itself (the model's own validators only enforce priority-band and mode
# validity — see that module's docstring).
_SCOPE_REQUIREMENTS: dict[str, str] = {
    "SITE_CUSTOM": "site_id",
    "MANAGER_CUSTOM": "manager_type",
    "VENDOR_CUSTOM": "vendor",
}
_KNOWN_SOURCES = frozenset(
    {"SITE_CUSTOM", "MANAGER_CUSTOM", "VENDOR_CUSTOM", "GLOBAL_CUSTOM", "SYSTEM_DEFAULT"}
)


def _validate_scope_source_coherence(policy: HealthPolicy) -> None:
    source = policy.source
    scope = policy.scope

    if source not in _KNOWN_SOURCES:
        raise ValidationAppError(f"Unknown source {source!r}.", details={"source": source})

    required_field = _SCOPE_REQUIREMENTS.get(source)
    if required_field is not None and getattr(scope, required_field) is None:
        raise ValidationAppError(
            f"{source} policies must set scope.{required_field}.",
            details={"source": source, "missing_field": required_field},
        )

    if source == "GLOBAL_CUSTOM" and (
        scope.site_id is not None or scope.manager_type is not None or scope.vendor is not None
    ):
        raise ValidationAppError(
            "GLOBAL_CUSTOM policies must not set any scope field.",
            details={"source": source, "scope": scope.model_dump()},
        )


def validate_policy_write(policy: HealthPolicy, *, registry: MetricRegistry) -> None:
    """Everything the domain model's own `model_validator`s don't already
    enforce (priority-band, mode validity — see `HealthPolicy`'s
    docstring): condition safety against the metric registry, template
    safety against the declared evidence keys, and source/scope
    coherence. Called by the API layer before every create/update.

    `ConditionValidationError` messages are pattern-matched to decide
    between the three health-policy error codes that already exist for
    condition problems (`UNKNOWN_METRIC`, `METRIC_OPERATOR_MISMATCH`,
    generic `CONDITION_INVALID`) — the domain layer raises one exception
    type for all of these, so message content is the only signal
    available here without changing that (fixed, read-only) contract.
    """
    try:
        validate_condition(policy.condition, registry)
    except ConditionValidationError as exc:
        message = str(exc)
        if message.startswith("unknown metric"):
            raise UnknownMetricError(message) from exc
        if "is not valid for metric type" in message:
            raise MetricOperatorMismatchError(message) from exc
        raise ConditionInvalidError(message) from exc

    try:
        validate_template(policy.message_template, {e.key for e in policy.evidence})
    except TemplateValidationError as exc:
        raise TemplateInvalidError(str(exc)) from exc

    _validate_scope_source_coherence(policy)


def validate_system_field_lock(*, existing: HealthPolicy, updates: dict[str, Any]) -> None:
    """A `system=True` policy may only have its `enabled` flag changed —
    everything else about a shipped default is fixed. `updates` is the
    caller's partial-update payload keyed by field name (e.g. from
    `HealthPolicyUpdate.model_dump(exclude_unset=True)`).
    """
    if not existing.system:
        return
    disallowed = set(updates) - {"enabled"}
    if disallowed:
        raise ValidationAppError(
            "Only 'enabled' may be changed on a system health policy.",
            details={"policy_id": existing.id, "disallowed_fields": sorted(disallowed)},
        )


@dataclass(frozen=True, slots=True)
class PreviewMatch:
    id: str
    name: str
    would_be_severity: HealthSeverity


@dataclass(frozen=True, slots=True)
class PreviewResult:
    matched_count: int
    truncated: bool
    sample: list[PreviewMatch] = field(default_factory=list)
    mode: str = "sampled"


def _build_draft_policy(draft_policy_input: dict[str, Any]) -> HealthPolicy:
    """Turn the API layer's raw draft payload into a validated (but never
    persisted) `HealthPolicy`. Handles the same two defaults the create
    route applies to a real policy — a generated id when none was
    supplied (a preview of a brand-new, not-yet-saved policy), and
    `policy_key` defaulting to that id (see `HealthPolicy`'s module
    docstring) — so a preview of an unsaved draft behaves identically to
    what saving it would produce.
    """
    payload = dict(draft_policy_input)
    is_edit = payload.get("id") is not None or payload.get("_id") is not None
    if not is_edit:
        payload.setdefault("_id", new_id("health_policy"))
    if not payload.get("policy_key"):
        payload["policy_key"] = payload.get("_id") or payload.get("id")
    now = utcnow()
    payload.setdefault("created_at", now)
    payload.setdefault("updated_at", now)
    try:
        return HealthPolicy.model_validate(payload)
    except PydanticValidationError as exc:
        # See `app.api.v1.health_policies._validate_and_build`'s comment:
        # a model-level validator error embeds the whole payload
        # (`datetime` fields included) as `input`, and can carry the
        # raised `ValueError` itself in `ctx` — neither is
        # JSON-serializable for the RFC 9457 response body.
        raise ValidationAppError(
            "Draft health policy failed validation.",
            details={
                "errors": exc.errors(include_input=False, include_url=False, include_context=False)
            },
        ) from exc


class HealthPolicyService:
    """The only place `extract_facts` + policy loading + `evaluate_health`
    are wired together — callers never assemble those three steps
    themselves. See module docstring for the `server_repo` constructor
    deviation from the task brief's literal signature.
    """

    def __init__(
        self,
        *,
        policy_repo: MongoHealthPolicyRepository,
        registry: MetricRegistry,
        server_repo: MongoServerRepository,
    ) -> None:
        self._policy_repo = policy_repo
        self._registry = registry
        self._server_repo = server_repo

    async def evaluate_server(self, server: Server) -> HealthState:
        """Load every stored policy (scope filtering happens inside
        `evaluate_health` itself — see its docstring) and evaluate against
        this server's facts. `manager_type` is always `None`: `Server`
        carries no `manager_type` field today (only `manager_id` — see
        `app.domain.models.server.Server`), so any policy scoped to a
        `manager_type` cannot currently match any server. That's a known
        gap in the `Server` schema, not something this service can paper
        over.
        """
        facts = extract_facts(server)
        policies = await self._policy_repo.list_all()
        return evaluate_health(
            facts,
            policies,
            self._registry,
            vendor=server.identity.vendor.value,
            manager_type=None,
            site_id=server.site_id,
        )

    async def preview(
        self,
        draft_policy_input: dict[str, Any],
        *,
        sample_size: int = 50,
        max_scan: int = 5000,
    ) -> PreviewResult:
        """Whether a DRAFT policy (not yet saved) would be the effective,
        FIRING evaluation for its `policy_key` family, per scope-matching
        candidate server — not just "does the condition match in
        isolation". A low-priority/low-specificity draft with a matching
        condition can still be shadowed by an existing higher-precedence
        family member for a given server (see
        `app.domain.services.health.evaluate.resolve_families`), so the
        only correct way to answer "would this draft actually fire" is to
        re-run the real resolution + evaluation with the draft spliced
        into the full policy set.

        Cost / implementation notes (read before raising `max_scan` in a
        caller): this is *materially* more expensive than the
        classification-rule preview, because there is no way to
        pre-filter "would this condition match" in Mongo — conditions are
        an arbitrary boolean tree over metrics resolved from nested
        server sub-documents, not a single indexable field. Every
        candidate server costs one full `evaluate_health()` call over the
        *entire* combined policy set (existing policies plus the draft),
        not just the draft's own condition. `max_scan` bounds a Mongo
        query (`identity.vendor`/`site_id`, whichever the draft's scope
        sets) to keep this from becoming an unbounded collection scan;
        `sample_size` only bounds how many matches are *returned*, not how
        many are scanned. `mode` is always `"sampled"` — compiling a
        Mongo-native version of the condition tree so preview could be a
        single aggregation instead of N re-evaluations is a documented
        future enhancement (see task brief), not attempted here.
        """
        draft = _build_draft_policy(draft_policy_input)
        validate_policy_write(draft, registry=self._registry)

        existing = await self._policy_repo.list_all(enabled_only=False)
        existing = [p for p in existing if p.id != draft.id]
        combined = [*existing, draft]

        mongo_filters: dict[str, Any] = {}
        if draft.scope.vendor is not None:
            mongo_filters["identity.vendor"] = draft.scope.vendor
        if draft.scope.site_id is not None:
            mongo_filters["site_id"] = draft.scope.site_id
        # `scope.manager_type` cannot be pushed into this query — `Server`
        # carries no `manager_type` field (see `evaluate_server`'s
        # docstring for the same gap).

        page = await self._server_repo.list_page(
            filters=mongo_filters,
            search=None,
            sort="name",
            sort_desc=False,
            cursor=None,
            page_size=max_scan,
            with_count=False,
        )
        candidates = page.items
        truncated = page.has_more

        matched_count = 0
        sample: list[PreviewMatch] = []
        for server in candidates:
            facts = extract_facts(server)
            state = evaluate_health(
                facts,
                combined,
                self._registry,
                vendor=server.identity.vendor.value,
                manager_type=None,
                site_id=server.site_id,
            )
            firing = next(
                (e for e in state.evaluations if e.policy_id == draft.id and e.active), None
            )
            if firing is None:
                continue
            matched_count += 1
            if len(sample) < sample_size:
                sample.append(
                    PreviewMatch(id=server.id, name=server.name, would_be_severity=firing.severity)
                )

        return PreviewResult(
            matched_count=matched_count, truncated=truncated, sample=sample, mode="sampled"
        )
