"""`ClassificationService`: the one integration seam for slice 2's rule
engine.

Two ways to reach the domain's pure `classify()` function, and both are
this module's job precisely so a caller never has to call `list_all()`
and `classify()` separately — that split is exactly the kind of
duplication that lets a caller forget the `enabled_only` filter:

- `classify_server` loads the active ruleset fresh (all enabled rules,
  via `MongoClassificationRuleRepository.list_all(enabled_only=True)`)
  on every call — the right choice for a single, standalone
  classification against whatever is current right now (the
  `POST /servers/{id}/reclassify` route).
- `load_ruleset` + `classify_with_ruleset` split that same load out from
  the classification: a caller classifying many servers in one run (the
  ingestion pipeline, per `app.application.services.ingest`) loads the
  ruleset once — P1 in `docs/notes/2026-09-audit.md`: a 10,000-server run
  was issuing ~10,000 uncached collection reads for an answer that cannot
  change during the run — and calls `classify_with_ruleset` per server
  against that one snapshot. `enabled_only=True` still lives in exactly
  one place (`load_ruleset`), so this isn't the duplication the split
  above warns about.

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

        Args:
            classifiable (ClassifiableServer): The server to classify.

        Returns:
            ClassificationResult: The resolved installation type and, when
                one matched, which rule and why.
        """
        rules = await self.load_ruleset()
        return self.classify_with_ruleset(classifiable, rules)

    async def load_ruleset(self) -> list[ClassificationRule]:
        """The current active (enabled) ruleset, for a caller that will
        classify many servers against the same snapshot in one run — the
        ingestion pipeline, never a fresh `classify_server` call repeated
        per server. Centralizing `enabled_only=True` here rather than
        letting a caller reach for `list_all()` directly is what keeps
        this method (and `classify_server`) the only two places that
        filter can be forgotten.

        Returns:
            list[ClassificationRule]: The rules to pass into
                `classify_with_ruleset` for the rest of the run.
        """
        return await self._rule_repo.list_all(enabled_only=True)

    def classify_with_ruleset(
        self, classifiable: ClassifiableServer, ruleset: list[ClassificationRule]
    ) -> ClassificationResult:
        """Classify against an already-loaded ruleset (`load_ruleset`) —
        the ingest-loop counterpart to `classify_server`, which loads its
        own ruleset on every call. A caller classifying many servers in
        one run should call `load_ruleset` once and this per server, never
        `classify_server` in a loop.

        Args:
            classifiable (ClassifiableServer): The server to classify.
            ruleset (list[ClassificationRule]): The rules loaded by
                `load_ruleset` for this run.

        Returns:
            ClassificationResult: The resolved installation type and, when
                one matched, which rule and why.
        """
        return classify(classifiable, ruleset, self._engine)
