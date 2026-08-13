"""`ClassificationService`: the one integration seam for slice 2's rule
engine.

Two responsibilities, deliberately kept in one class because they share
the same `RegexEngine` and the same write-time validation contract:

1. `classify_server` — the ONLY place that loads the active ruleset (all
   enabled rules, via `MongoClassificationRuleRepository.list_all
   (enabled_only=True)`) and hands it to the pure domain `classify()`
   function. Callers (a later slice's ingestion wiring) never call
   `list_all()` and `classify()` separately — that split is exactly the
   kind of duplication that lets a caller forget the `enabled_only` filter.
2. `preview` — reports which *existing* servers a *draft* (unsaved) rule
   would match, without ever persisting anything or sending the user's
   raw pattern into a MongoDB `$regex` query. Regex evaluation happens
   only in Python, via the same `RegexEngine.search()` the real
   classifier uses — MongoDB's job here is only to cheaply narrow the
   candidate set by the parts of `scope` it *can* express as an indexed
   equality filter (vendor, site_id).

   Known limitation, documented rather than silently wrong: `Server` has
   no `manager_type` field of its own (only `manager_id` — see
   `app.domain.models.server.Identity`/`Server`), so a draft rule scoped
   to `scope.manager_type` cannot be narrowed by that dimension in the
   Mongo query without an extra lookup against the managers collection
   per candidate. `preview()` deliberately skips filtering on it rather
   than adding that per-candidate join — the real `classify()` resolution
   path (fed `ClassifiableServer.manager_type` by the ingestion pipeline)
   still scopes correctly at classification time; this only means a
   manager-scoped draft's preview candidate set is wider than its eventual
   live match set.

`validate_rule_write` is a free function, not a method, because both the
create and update code paths in `app.api.v1.classification_rules` need to
run it against a fully-merged `ClassificationRule` before persisting —
there's no partial-update variant of this validation, only "validate the
rule that would result".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pymongo.asynchronous.collection import AsyncCollection

from app.domain.models.classification_rule import (
    CLASSIFIABLE_FIELDS,
    PRIORITY_BANDS,
    ClassificationRule,
)
from app.domain.models.server import Server
from app.domain.ports.regex_engine import RegexEngine, RegexTimeout, RegexUnsafeError
from app.domain.services.classification import ClassifiableServer, ClassificationResult, classify
from app.errors import (
    RegexInvalidAppError,
    RegexUnsafeAppError,
    RuleScopeInvalidError,
    ValidationAppError,
)
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import SERVERS_COLLECTION

_SITE_CUSTOM = "SITE_CUSTOM"
_MANAGER_CUSTOM = "MANAGER_CUSTOM"
_VENDOR_CUSTOM = "VENDOR_CUSTOM"
_GLOBAL_CUSTOM = "GLOBAL_CUSTOM"
_SYSTEM_DEFAULT = "SYSTEM_DEFAULT"
_UNSCOPED_SOURCES = frozenset({_GLOBAL_CUSTOM, _SYSTEM_DEFAULT})

_Document = dict[str, Any]


@dataclass(frozen=True, slots=True)
class PreviewResult:
    matched_count: int
    truncated: bool
    sample: list[dict[str, str]]
    mode: str = "sampled"


def validate_rule_write(
    rule: ClassificationRule, engine: RegexEngine, *, is_create: bool = False
) -> None:
    """Cross-field business-rule validation run before a rule (create or a
    merged update) is persisted. Raises the appropriate `AppError`
    subclass on the first violation found; never returns a value.
    """
    if is_create and rule.source == _SYSTEM_DEFAULT:
        # SYSTEM_DEFAULT rules are seeded (see `default_system_rules` in
        # `app.infrastructure.mongodb.classification_rule_repository`),
        # never authored through the API.
        raise RuleScopeInvalidError(
            "SYSTEM_DEFAULT rules cannot be created via the API; they are seeded.",
            details={"source": rule.source},
        )

    band = PRIORITY_BANDS.get(rule.source)
    if band is None:
        raise RuleScopeInvalidError(
            f"Unknown rule source {rule.source!r}. Must be one of {sorted(PRIORITY_BANDS)}.",
            details={"source": rule.source},
        )
    low, high = band
    if not (low <= rule.priority <= high):
        raise RuleScopeInvalidError(
            f"priority {rule.priority} is out of band for source {rule.source!r}: "
            f"must be between {low} and {high} inclusive.",
            details={
                "source": rule.source,
                "priority": rule.priority,
                "band_low": low,
                "band_high": high,
            },
        )

    scope = rule.scope
    if rule.source == _SITE_CUSTOM and scope.site_id is None:
        raise RuleScopeInvalidError(
            "SITE_CUSTOM rules require scope.site_id to be set.",
            details={"source": rule.source},
        )
    if rule.source == _MANAGER_CUSTOM and scope.manager_type is None:
        raise RuleScopeInvalidError(
            "MANAGER_CUSTOM rules require scope.manager_type to be set.",
            details={"source": rule.source},
        )
    if rule.source == _VENDOR_CUSTOM and scope.vendor is None:
        raise RuleScopeInvalidError(
            "VENDOR_CUSTOM rules require scope.vendor to be set.",
            details={"source": rule.source},
        )
    if rule.source in _UNSCOPED_SOURCES and (
        scope.vendor is not None or scope.manager_type is not None or scope.site_id is not None
    ):
        raise RuleScopeInvalidError(
            f"{rule.source} rules must have an empty scope "
            "(vendor, manager_type, and site_id all null).",
            details={"source": rule.source},
        )

    if rule.field not in CLASSIFIABLE_FIELDS:
        raise ValidationAppError(
            f"field must be one of {sorted(CLASSIFIABLE_FIELDS)}, got {rule.field!r}.",
            details={"field": rule.field, "allowed": sorted(CLASSIFIABLE_FIELDS)},
        )

    _validate_pattern(
        rule.pattern,
        engine,
        ignore_case=rule.flags.ignore_case,
        multiline=rule.flags.multiline,
        dotall=rule.flags.dotall,
    )


def _validate_pattern(
    pattern: str, engine: RegexEngine, *, ignore_case: bool, multiline: bool, dotall: bool
) -> None:
    try:
        engine.validate(pattern, ignore_case=ignore_case, multiline=multiline, dotall=dotall)
    except RegexUnsafeError as exc:
        raise RegexUnsafeAppError(str(exc), details={"pattern": pattern}) from exc
    except Exception as exc:  # defensive: any other regex-compile error the
        # engine implementation might let through, not just RegexUnsafeError.
        raise RegexInvalidAppError(str(exc), details={"pattern": pattern}) from exc


class ClassificationService:
    def __init__(
        self,
        *,
        rule_repo: MongoClassificationRuleRepository,
        engine: RegexEngine,
        mongo: MongoClientHolder,
    ) -> None:
        self._rule_repo = rule_repo
        self._engine = engine
        self._mongo = mongo

    @property
    def _servers_collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[SERVERS_COLLECTION]

    async def classify_server(self, classifiable: ClassifiableServer) -> ClassificationResult:
        """Load every enabled rule and resolve `classifiable` against it.
        Quarantined rules are excluded by the domain `classify()` function
        itself (see its own predicate), not pre-filtered here — this
        method never re-implements that predicate, it only supplies the
        enabled ruleset.
        """
        rules = await self._rule_repo.list_all(enabled_only=True)
        return classify(classifiable, rules, self._engine)

    async def preview(
        self,
        draft_rule_input: dict[str, Any],
        *,
        sample_size: int = 50,
        max_scan: int = 5000,
    ) -> PreviewResult:
        field_name = draft_rule_input.get("field")
        pattern = draft_rule_input.get("pattern")

        if field_name not in CLASSIFIABLE_FIELDS:
            raise ValidationAppError(
                f"field must be one of {sorted(CLASSIFIABLE_FIELDS)}, got {field_name!r}.",
                details={"field": field_name, "allowed": sorted(CLASSIFIABLE_FIELDS)},
            )
        if not isinstance(pattern, str) or not pattern:
            raise ValidationAppError("pattern is required.", details={"pattern": pattern})

        flags_in = draft_rule_input.get("flags") or {}
        ignore_case = bool(flags_in.get("ignore_case", True))
        multiline = bool(flags_in.get("multiline", False))
        dotall = bool(flags_in.get("dotall", False))

        _validate_pattern(
            pattern, self._engine, ignore_case=ignore_case, multiline=multiline, dotall=dotall
        )

        scope_in = draft_rule_input.get("scope") or {}
        mongo_filter: dict[str, object] = {}
        vendor = scope_in.get("vendor")
        if vendor is not None:
            mongo_filter["identity.vendor"] = vendor
        site_id = scope_in.get("site_id")
        if site_id is not None:
            mongo_filter["site_id"] = site_id
        # scope.manager_type intentionally NOT applied here — see module
        # docstring's "known limitation" note.

        raw_docs = await (
            self._servers_collection.find(mongo_filter).limit(max_scan).to_list(length=max_scan)
        )
        truncated = len(raw_docs) >= max_scan

        matched_count = 0
        sample: list[dict[str, str]] = []
        for doc in raw_docs:
            server = Server.model_validate(doc)
            value = _extract_field(server, field_name)
            if value is None:
                continue
            try:
                match = self._engine.search(
                    pattern, value, ignore_case=ignore_case, multiline=multiline, dotall=dotall
                )
            except RegexTimeout:
                continue
            if match is None:
                continue
            matched_count += 1
            if len(sample) < sample_size:
                sample.append({"id": server.id, "name": server.name})

        return PreviewResult(
            matched_count=matched_count, truncated=truncated, sample=sample, mode="sampled"
        )


def _extract_field(server: Server, field_name: str) -> str | None:
    """Pull a `CLASSIFIABLE_FIELDS` value off a real `Server` document.
    `hostname` is deliberately unmapped (always `None`, so a draft rule on
    it always previews zero matches rather than erroring): `Server` has no
    `hostname` field today (see `app.domain.models.server`) — only `name`,
    `identity.serial`, `model`, and `site_id` are actually extractable.
    """
    if field_name == "name":
        return server.name
    if field_name == "serial":
        return server.identity.serial
    if field_name == "model":
        return server.model
    if field_name == "site_id":
        return server.site_id
    return None
