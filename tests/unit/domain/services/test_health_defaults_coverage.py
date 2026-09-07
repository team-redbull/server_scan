"""The seeded checks, and which servers they can actually fire for.

Two things are pinned here. Every default policy's condition must
reference a registered metric — a policy referencing an unknown metric is
rejected at save time, so a seeded one that did would fail at startup
rather than at write.

And the vendor-neutrality claim: a check that only ever fires for one
vendor is not fleet coverage. `extract_facts` is fed servers built the way
each provider builds them, and the assertion is on what the facts say.
"""

from __future__ import annotations

from app.domain.enums import HealthSeverity, LinkState
from app.domain.models.hardware import Gpu, Hardware, Power, Psu, Storage, StorageDrive
from app.domain.models.network import NetworkInfo, NetworkInterface
from app.domain.models.server import Server
from app.domain.services.health.facts import extract_facts
from app.domain.services.health.health_policy_defaults import default_system_policies
from app.domain.services.health.metrics import build_default_registry


def _metrics_in(condition: object) -> list[str]:
    """
    Every metric name a condition tree references.

    Args:
        condition (object): A `Condition`, or None.

    Returns:
        list[str]: Metric names, including those inside all_of/any_of/not.
    """
    if condition is None:
        return []
    names: list[str] = []
    metric = getattr(condition, "metric", None)
    if metric:
        names.append(str(metric))
    for key in ("all_of", "any_of"):
        for child in getattr(condition, key, None) or []:
            names.extend(_metrics_in(child))
    names.extend(_metrics_in(getattr(condition, "not_", None)))
    return names


def _server(**hardware: object) -> Server:
    """
    A server carrying only the hardware a test cares about.

    Args:
        **hardware (object): Fields to set on `Hardware`/`NetworkInfo`.

    Returns:
        Server: A minimally-populated server.
    """
    network = hardware.pop("network", NetworkInfo())
    return Server.model_construct(
        # `model_construct` skips validation, so a required field left out is
        # simply absent and reading it raises. `name` is read by the 5TB/10TB
        # facts; blank means neither rule fires, which is what these tests want.
        name="",
        hardware=Hardware(**hardware),  # ty: ignore[invalid-argument-type]
        network=network,
    )


class TestSeededPolicies:
    """What ships in `default_system_policies()`."""

    def test_every_condition_references_a_registered_metric(self) -> None:
        """A policy naming an unknown metric is rejected on save, so a
        seeded one that did would break startup seeding rather than fail
        quietly.
        """
        registry = build_default_registry()
        for policy in default_system_policies():
            for metric in _metrics_in(policy.condition):
                assert metric in registry, f"{policy.policy_key} references unknown {metric!r}"

    def test_every_evidence_field_references_a_registered_metric(self) -> None:
        """Evidence is interpolated into the message; an unknown metric
        would render an empty finding message.
        """
        registry = build_default_registry()
        for policy in default_system_policies():
            for field in policy.evidence:
                assert field.metric in registry

    def test_policy_keys_are_unique(self) -> None:
        """Same `policy_key` means "these compete for one winner"
        (ADR-0005). Two defaults sharing one would silently shadow each
        other, which is exactly what the two fabric policies avoid by
        using different keys.
        """
        keys = [p.policy_key for p in default_system_policies()]
        assert len(keys) == len(set(keys))


class TestCoverageAcrossVendors:
    """A check that only fires for one vendor is not fleet coverage."""

    def test_a_failed_psu_is_seen_whichever_vocabulary_reported_it(self) -> None:
        """Every provider normalizes a PSU onto UP/DOWN, so one fact
        covers UCS, Intersight and Redfish alike.
        """
        server = _server(power=Power(psus=[Psu(id="0", health="UP"), Psu(id="1", health="DOWN")]))
        assert extract_facts(server)["power.failed_psu_count"] == 1

    def test_a_failed_gpu_is_seen_in_both_vocabularies(self) -> None:
        """Redfish maps GPU health onto HealthSeverity (CRITICAL) while
        UCS and Intersight map OperState (DOWN). Counting only one spelling
        would cover one vendor and silently miss the other.
        """
        redfish_style = _server(gpus=[Gpu(health=HealthSeverity.CRITICAL.value)])
        cisco_style = _server(gpus=[Gpu(health="DOWN")])
        assert extract_facts(redfish_style)["gpu.failed_count"] == 1
        assert extract_facts(cisco_style)["gpu.failed_count"] == 1

    def test_a_healthy_gpu_counts_in_neither(self) -> None:
        """The mirror of the above: UP and HEALTHY must both stay clear."""
        for healthy in ("UP", HealthSeverity.HEALTHY.value):
            assert extract_facts(_server(gpus=[Gpu(health=healthy)]))["gpu.failed_count"] == 0

    def test_a_degraded_drive_is_counted_apart_from_a_failed_one(self) -> None:
        """The WARNING check must not swallow the CRITICAL one, or a dead
        drive would downgrade to a warning.
        """
        server = _server(
            storage=Storage(
                drives=[
                    StorageDrive(id="0", health=HealthSeverity.WARNING.value),
                    StorageDrive(id="1", health=HealthSeverity.CRITICAL.value),
                ]
            )
        )
        facts = extract_facts(server)
        assert facts["storage.warning_drive_count"] == 1
        assert facts["storage.failed_drive_count"] == 1


class TestNoFalseAlarms:
    """The conditions most likely to alert on healthy hardware."""

    def test_unused_nics_do_not_trip_the_link_check(self) -> None:
        """A server with one uplink and three unused ports is healthy. This
        is why the fact counts links UP rather than links down.
        """
        server = _server(
            network=NetworkInfo(
                interfaces=[
                    NetworkInterface(name="a", link_state=LinkState.UP),
                    NetworkInterface(name="b", link_state=LinkState.DOWN),
                    NetworkInterface(name="c", link_state=LinkState.DOWN),
                ]
            )
        )
        facts = extract_facts(server)
        assert facts["network.links_up_count"] == 1
        assert facts["network.interface_count"] == 3

    def test_a_server_with_no_interfaces_read_is_not_all_links_down(self) -> None:
        """`interface_count GTE 1` is what separates "every link is down"
        from "nothing was reported" — a collection gap must not alert.
        """
        facts = extract_facts(_server())
        assert facts["network.interface_count"] == 0
        assert facts["network.links_up_count"] == 0

    def test_correctable_gpu_errors_are_not_counted(self) -> None:
        """A correctable ECC error is the mechanism working as designed.
        Counting it would fire on healthy accelerators constantly.
        """
        server = _server(gpus=[Gpu(correctable_error_count=4096, uncorrectable_error_count=0)])
        assert extract_facts(server)["gpu.uncorrectable_error_count"] == 0

    def test_uncorrectable_errors_sum_across_cards(self) -> None:
        """One policy on the server total is what gets alerted on; the
        per-card detail is on the document for whoever investigates.
        """
        server = _server(
            gpus=[Gpu(uncorrectable_error_count=1), Gpu(uncorrectable_error_count=2), Gpu()]
        )
        assert extract_facts(server)["gpu.uncorrectable_error_count"] == 3

    def test_a_server_with_no_gpus_reports_zero_not_a_failure(self) -> None:
        """Most of the fleet has no GPU at all; the GPU checks must be
        silent there rather than firing on absence.
        """
        facts = extract_facts(_server())
        assert facts["gpu.count"] == 0
        assert facts["gpu.failed_count"] == 0
