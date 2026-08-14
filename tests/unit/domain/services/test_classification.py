"""Classification resolution tests, including the platform spec's own
named acceptance scenario (system default vs. Dell vendor rules).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

from app.domain.enums import InstallationType, Vendor
from app.domain.models.classification_rule import ClassificationRule, RuleScope
from app.domain.ports.regex_engine import RegexMatch, RegexTimeout
from app.domain.services.classification import ClassifiableServer, classify
from app.domain.services.regex_engine import RegexModuleEngine

NOW = datetime.now(UTC)
ENGINE = RegexModuleEngine(max_pattern_length=200, match_timeout_seconds=0.25)


def _rule(
    rule_id: str,
    *,
    installation_type: InstallationType,
    pattern: str,
    field: str = "name",
    source: str = "SYSTEM_DEFAULT",
    priority: int = 100,
    order: int = 0,
    scope: RuleScope | None = None,
    enabled: bool = True,
) -> ClassificationRule:
    return ClassificationRule(
        _id=rule_id,
        name=rule_id,
        installation_type=installation_type,
        field=field,
        pattern=pattern,
        source=source,
        priority=priority,
        order=order,
        scope=scope or RuleScope(),
        enabled=enabled,
        created_at=NOW,
        updated_at=NOW,
    )


def _server(
    name: str, *, vendor: Vendor = Vendor.DELL, site_id: str | None = None
) -> ClassifiableServer:
    return ClassifiableServer(name=name, vendor=vendor, manager_type=None, site_id=site_id)


# --- Spec §70 acceptance scenario, verbatim ---

SYSTEM_HOSTED = _rule(
    "rule_sys_hosted",
    installation_type=InstallationType.HOSTED_CLUSTER,
    pattern=r"^ocp-.*",
    priority=100,
)
SYSTEM_UPI = _rule(
    "rule_sys_upi", installation_type=InstallationType.UPI, pattern=r"^upi-.*", priority=100
)
DELL_HOSTED = _rule(
    "rule_dell_hosted",
    installation_type=InstallationType.HOSTED_CLUSTER,
    pattern=r"^ocp-dell-.*",
    source="VENDOR_CUSTOM",
    priority=300,
    scope=RuleScope(vendor=Vendor.DELL),
)
DELL_UPI = _rule(
    "rule_dell_upi",
    installation_type=InstallationType.UPI,
    pattern=r"^upi-dell-.*",
    source="VENDOR_CUSTOM",
    priority=300,
    scope=RuleScope(vendor=Vendor.DELL),
)
ACCEPTANCE_RULES = [SYSTEM_HOSTED, SYSTEM_UPI, DELL_HOSTED, DELL_UPI]


def test_dell_worker_classified_hosted_cluster_via_dell_vendor_rule() -> None:
    result = classify(_server("ocp-dell-worker-001", vendor=Vendor.DELL), ACCEPTANCE_RULES, ENGINE)
    assert result.installation_type == InstallationType.HOSTED_CLUSTER
    assert result.rule_id == "rule_dell_hosted"


def test_dell_upi_classified_via_dell_vendor_rule() -> None:
    result = classify(_server("upi-dell-001", vendor=Vendor.DELL), ACCEPTANCE_RULES, ENGINE)
    assert result.installation_type == InstallationType.UPI
    assert result.rule_id == "rule_dell_upi"


def test_hp_hosted_falls_back_to_system_default() -> None:
    # HP has no vendor-scoped rule in this set, so the Dell-scoped rules
    # don't even match its scope — the system default is the only
    # candidate that reaches the pattern check.
    result = classify(_server("ocp-hp-001", vendor=Vendor.HP), ACCEPTANCE_RULES, ENGINE)
    assert result.installation_type == InstallationType.HOSTED_CLUSTER
    assert result.rule_id == "rule_sys_hosted"


def test_unmatched_name_is_unclassified() -> None:
    result = classify(_server("random-server", vendor=Vendor.DELL), ACCEPTANCE_RULES, ENGINE)
    assert result.installation_type == InstallationType.UNCLASSIFIED
    assert result.rule_id is None


# --- Precedence mechanics ---


def test_site_scope_beats_global_scope_at_equal_priority() -> None:
    global_rule = _rule(
        "rule_global", installation_type=InstallationType.UPI, pattern=r"^ocp-.*", priority=200
    )
    site_rule = _rule(
        "rule_site",
        installation_type=InstallationType.HOSTED_CLUSTER,
        pattern=r"^ocp-.*",
        priority=200,
        scope=RuleScope(site_id="site_lab"),
    )
    result = classify(_server("ocp-x", site_id="site_lab"), [global_rule, site_rule], ENGINE)
    assert result.installation_type == InstallationType.HOSTED_CLUSTER
    assert result.rule_id == "rule_site"


def test_disabled_rule_is_never_a_candidate() -> None:
    rule = _rule(
        "rule_x",
        installation_type=InstallationType.HOSTED_CLUSTER,
        pattern=r"^ocp-.*",
        enabled=False,
    )
    result = classify(_server("ocp-x"), [rule], ENGINE)
    assert result.installation_type == InstallationType.UNCLASSIFIED


def test_scope_mismatch_excludes_rule_from_candidates() -> None:
    cisco_only = _rule(
        "rule_cisco",
        installation_type=InstallationType.HOSTED_CLUSTER,
        pattern=r"^ocp-.*",
        source="VENDOR_CUSTOM",
        priority=300,
        scope=RuleScope(vendor=Vendor.CISCO),
    )
    result = classify(_server("ocp-dell-x", vendor=Vendor.DELL), [cisco_only], ENGINE)
    assert result.installation_type == InstallationType.UNCLASSIFIED


# --- Conflict detection ---


def test_equal_precedence_disagreement_is_recorded_as_conflict() -> None:
    a = _rule(
        "rule_a",
        installation_type=InstallationType.HOSTED_CLUSTER,
        pattern=r"^ocp-.*",
        priority=200,
    )
    b = _rule("rule_b", installation_type=InstallationType.UPI, pattern=r"^ocp-.*", priority=200)
    result = classify(_server("ocp-x"), [a, b], ENGINE)
    # Winner is deterministic (lowest id after order), but the disagreement
    # must still be surfaced, never silently dropped.
    assert result.rule_id == "rule_a"
    assert len(result.conflicts) == 1
    assert result.conflicts[0].rule_id == "rule_b"


def test_no_conflict_recorded_when_lower_precedence_rule_disagrees() -> None:
    winner = _rule(
        "rule_winner",
        installation_type=InstallationType.HOSTED_CLUSTER,
        pattern=r"^ocp-.*",
        priority=300,
    )
    loser = _rule(
        "rule_loser", installation_type=InstallationType.UPI, pattern=r"^ocp-.*", priority=100
    )
    result = classify(_server("ocp-x"), [winner, loser], ENGINE)
    assert result.rule_id == "rule_winner"
    assert result.conflicts == []


def test_resolution_is_deterministic_across_shuffled_input_order() -> None:
    rules = [SYSTEM_HOSTED, SYSTEM_UPI, DELL_HOSTED, DELL_UPI]
    server = _server("ocp-dell-worker-001", vendor=Vendor.DELL)
    baseline = classify(server, rules, ENGINE)
    rng = random.Random(1234)
    for _ in range(50):
        shuffled = rules.copy()
        rng.shuffle(shuffled)
        result = classify(server, shuffled, ENGINE)
        assert result.installation_type == baseline.installation_type
        assert result.rule_id == baseline.rule_id
        assert result.conflicts == baseline.conflicts


# --- Regex timeout handling ---


class _AlwaysTimeoutEngine:
    """Test double: every `search()` call times out, regardless of pattern."""

    def validate(self, pattern: str, *, ignore_case: bool, multiline: bool, dotall: bool) -> None:
        return None

    def search(
        self, pattern: str, subject: str, *, ignore_case: bool, multiline: bool, dotall: bool
    ) -> RegexMatch | None:
        raise RegexTimeout("simulated timeout")


def test_timing_out_rule_is_skipped_and_recorded_not_crashed() -> None:
    rule = _rule("rule_slow", installation_type=InstallationType.HOSTED_CLUSTER, pattern=r"^ocp-.*")
    result = classify(_server("ocp-x"), [rule], _AlwaysTimeoutEngine())
    assert result.installation_type == InstallationType.UNCLASSIFIED
    assert len(result.errors) == 1
    assert result.errors[0].rule_id == "rule_slow"


def test_timeout_on_losing_rule_does_not_prevent_a_match_from_a_working_rule() -> None:
    slow_rule = _rule(
        "rule_slow", installation_type=InstallationType.UPI, pattern=r"^upi-.*", priority=300
    )
    good_rule = _rule(
        "rule_good",
        installation_type=InstallationType.HOSTED_CLUSTER,
        pattern=r"^ocp-.*",
        priority=100,
    )

    class _MixedEngine:
        def validate(
            self, pattern: str, *, ignore_case: bool, multiline: bool, dotall: bool
        ) -> None:
            return None

        def search(
            self, pattern: str, subject: str, *, ignore_case: bool, multiline: bool, dotall: bool
        ) -> RegexMatch | None:
            if pattern == r"^upi-.*":
                raise RegexTimeout("simulated timeout")
            return ENGINE.search(
                pattern, subject, ignore_case=ignore_case, multiline=multiline, dotall=dotall
            )

    result = classify(_server("ocp-x"), [slow_rule, good_rule], _MixedEngine())
    assert result.installation_type == InstallationType.HOSTED_CLUSTER
    assert result.rule_id == "rule_good"
    assert len(result.errors) == 1


# --- Field extraction ---


def test_rule_on_absent_field_is_skipped_not_errored() -> None:
    rule = _rule(
        "rule_serial",
        installation_type=InstallationType.HOSTED_CLUSTER,
        pattern=r".*",
        field="serial",
    )
    result = classify(_server("ocp-x"), [rule], ENGINE)  # serial is None on this server
    assert result.installation_type == InstallationType.UNCLASSIFIED
    assert result.errors == []
