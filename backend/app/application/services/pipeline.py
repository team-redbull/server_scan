"""Maps engine results (`ClassificationResult`, `HealthState`) onto the
small embedded models a `Server` document actually persists
(`Classification`, `Health`).

A dedicated mapping module rather than inlining this in `ingest.py`: the
engines' result types carry engine-internal detail (conflicts, per-leaf
evidence, shadowed/suppressed families) that's useful for an audit trail
or a "why is this WARNING" UI panel later, but the embedded `Server.
classification`/`Server.health` fields are deliberately small summaries —
keeping the mapping in one place means that summary boundary is enforced
in exactly one spot, not re-decided at every call site.
"""

from __future__ import annotations

from app.domain.models.classification import Classification
from app.domain.models.health import Health
from app.domain.services.classification import ClassificationResult
from app.domain.services.health.evaluate import CATEGORIES, HealthState


def classification_from_result(
    result: ClassificationResult, *, previous_version: int
) -> Classification:
    return Classification(
        installation_type=result.installation_type,
        matched_rule_id=result.rule_id,
        matched_pattern=result.matched_pattern,
        matched_field=result.matched_field,
        classified_at=result.classified_at,
        classification_version=previous_version + 1,
    )


def health_from_state(state: HealthState) -> Health:
    severities = {cat: state.categories[cat].severity for cat in CATEGORIES}
    return Health(
        overall=state.overall,
        cpu=severities["cpu"],
        memory=severities["memory"],
        storage=severities["storage"],
        network=severities["network"],
        connectivity=severities["connectivity"],
        power=severities["power"],
        evaluated_at=state.evaluated_at,
    )
