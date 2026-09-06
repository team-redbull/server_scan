"""`ClassificationService`: the one integration seam for slice 2's rule
engine.

`classify_server` is the ONLY place that loads the active ruleset (all
enabled rules, via `MongoClassificationRuleRepository.list_all
(enabled_only=True)`) and hands it to the pure domain `classify()`
function. Callers (the ingestion pipeline) never call `list_all()` and
`classify()` separately — that split is exactly the kind of duplication
that lets a caller forget the `enabled_only` filter.

`validate_rule_write` is a free function, not a method: it validates a
fully-merged `ClassificationRule` before persisting, with no
partial-update variant — "validate the rule that would result". Its only
caller today is `app.application.services.bootstrap`, validating the
shipped system defaults at startup. The create/update API routes it was
originally written for are gone — rules are read-only now (`27b20a8`,
`f9ab059`) — and since bootstrap seeds the *entire* population of rules
that can ever exist, validating there is total coverage rather than a
narrower substitute.

A `preview()` method used to live here too — reporting which existing
servers a draft, unsaved rule would match, for the classification-rule
editor UI. Deleted along with that UI and its `/preview` endpoint: zero
callers were left once the editors went (see `f9ab059`'s commit message).
"""

from __future__ import annotations

from app.domain.models.classification_rule import (
    CLASSIFIABLE_FIELDS,
    PRIORITY_BANDS,
    ClassificationRule,
)
from app.domain.ports.regex_engine import RegexEngine, RegexUnsafeError
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

_SITE_CUSTOM = "SITE_CUSTOM"
_MANAGER_CUSTOM = "MANAGER_CUSTOM"
_VENDOR_CUSTOM = "VENDOR_CUSTOM"
_GLOBAL_CUSTOM = "GLOBAL_CUSTOM"
_SYSTEM_DEFAULT = "SYSTEM_DEFAULT"
_UNSCOPED_SOURCES = frozenset({_GLOBAL_CUSTOM, _SYSTEM_DEFAULT})


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
    ) -> None:
        self._rule_repo = rule_repo
        self._engine = engine

    async def classify_server(self, classifiable: ClassifiableServer) -> ClassificationResult:
        """Load every enabled rule and resolve `classifiable` against it.
        Quarantined rules are excluded by the domain `classify()` function
        itself (see its own predicate), not pre-filtered here — this
        method never re-implements that predicate, it only supplies the
        enabled ruleset.
        """
        rules = await self._rule_repo.list_all(enabled_only=True)
        return classify(classifiable, rules, self._engine)
