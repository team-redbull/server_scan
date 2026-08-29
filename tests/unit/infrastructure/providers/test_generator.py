"""Pure-logic tests for the deterministic fake data generator — no I/O,
no database, safe for `tests/unit`.
"""

from __future__ import annotations

from tools.run_collector import manager_for

from app.domain.enums import HealthSeverity, ManagerType, SiteCode, Vendor
from app.domain.models.hardware import Gpu
from app.domain.ports.credentials import ManagerConnection
from app.domain.value_objects.site import parse_site_code
from app.infrastructure.providers.fake.generator import (
    collector_for,
    generate_servers,
    list_managers,
    list_sites,
    manager_id_for,
    provider_type_for,
)


def test_same_seed_produces_byte_identical_output() -> None:
    first = list(generate_servers(seed=42, count=200))
    second = list(generate_servers(seed=42, count=200))
    assert first == second


def test_different_seed_produces_different_output() -> None:
    first = list(generate_servers(seed=1, count=50))
    second = list(generate_servers(seed=2, count=50))
    assert first != second


def test_count_is_respected() -> None:
    servers = list(generate_servers(seed=42, count=37))
    assert len(servers) == 37


def test_external_ids_are_unique() -> None:
    servers = list(generate_servers(seed=42, count=500))
    external_ids = [s.external_id for s in servers]
    assert len(external_ids) == len(set(external_ids))


def test_serials_are_unique_per_vendor() -> None:
    servers = list(generate_servers(seed=42, count=500))
    seen: set[tuple[str, str | None]] = set()
    for s in servers:
        key = (s.vendor, s.serial)
        assert key not in seen
        seen.add(key)


def test_system_uuids_are_unique() -> None:
    servers = list(generate_servers(seed=42, count=500))
    uuids = [s.system_uuid for s in servers]
    assert len(uuids) == len(set(uuids))


def test_vendors_are_from_the_known_set() -> None:
    servers = list(generate_servers(seed=42, count=200))
    vendors = {s.vendor for s in servers}
    assert vendors <= {v.value for v in Vendor}
    # `standalone` is a real Redfish-collected vendor, not a fallback, and
    # the UI filters on it — so it needs fixtures like any other.
    assert "standalone" in vendors


def test_bmc_address_forms_match_the_collector_that_reported_them() -> None:
    """The address shape is the collector's, not the vendor's: UCS reports
    `ipmi://host:623`, Redfish composes a `redfish://` system URL.
    """
    servers = list(generate_servers(seed=42, count=300))
    for s in servers:
        assert s.bmc_address_raw is not None
        if s.vendor == "cisco":
            assert s.bmc_address_raw.startswith("ipmi://")
            assert s.bmc_address_raw.endswith(":623")
        else:
            assert s.bmc_address_raw.startswith("redfish://")
            assert s.bmc_address_raw.endswith("/redfish/v1/Systems/1")


def test_external_ids_match_the_collector_that_reported_them() -> None:
    """Each collector stamps its own identity: a Central-rooted DN, an
    `intersight/<moid>`, or a Redfish system URL — the same strings the
    real collectors correlate on.
    """
    for s in generate_servers(seed=42, count=300):
        collector = provider_type_for(s)
        if collector == ManagerType.UCS_CENTRAL.value:
            assert s.external_id.startswith("compute/sys-")
            assert "/blade-" in s.external_id or "/rack-unit-" in s.external_id
        elif collector == ManagerType.INTERSIGHT.value:
            assert s.external_id.startswith("intersight/")
            # A Moid is 24 hex characters, which is what the real
            # `intersight.mapping.external_id` carries.
            assert len(s.external_id[len("intersight/") :]) == 24
        else:
            assert s.external_id.startswith("redfish://")


def test_only_ucs_central_servers_carry_a_service_profile_dn() -> None:
    """`profile_dn` is UCS Manager's alone. A BMC knows nothing about
    service profiles, and an Intersight `server.Profile` has no `Dn`
    field at all — so only UCS Central's servers get the org path that
    the site falls back to when a name carries no site token.
    """
    for s in generate_servers(seed=42, count=300):
        if provider_type_for(s) != ManagerType.UCS_CENTRAL.value:
            assert s.profile_dn is None
            continue
        assert s.profile_dn is not None
        assert s.profile_dn.startswith("org-root/org-")
        assert parse_site_code(s.profile_dn) is not None


def test_a_siteless_ucs_central_name_still_resolves_through_its_org_dn() -> None:
    """Only for UCS Central: it is the one collector with an org path to
    fall back to. An Intersight server whose name carries no site token
    really does end up unsited, which is a gap this fixture shows rather
    than papers over.
    """
    siteless = [
        s
        for s in generate_servers(seed=42, count=300)
        if provider_type_for(s) == ManagerType.UCS_CENTRAL.value and parse_site_code(s.name) is None
    ]
    assert siteless, "the siteless name family should reach UCS Central servers too"
    for s in siteless:
        assert parse_site_code(s.profile_dn) is not None


def test_each_collector_reports_gpus_to_its_own_ceiling() -> None:
    """`None` and `()` are different claims, and so are "the GPU exists"
    and "here is its temperature". UCS Central reads no GPU objects at
    all; Intersight reads a card's identity but has no telemetry field
    anywhere in its schema; only Redfish reads both.
    """
    servers = list(generate_servers(seed=42, count=300))
    by_collector: dict[str, list] = {}
    for s in servers:
        by_collector.setdefault(provider_type_for(s), []).append(s)

    assert all(s.gpus is None for s in by_collector[ManagerType.UCS_CENTRAL.value])
    assert all(s.gpus is not None for s in by_collector[ManagerType.INTERSIGHT.value])
    assert all(s.gpus is not None for s in by_collector[ManagerType.REDFISH_STANDALONE.value])

    with_gpus = [s for s in servers if s.gpus]
    assert with_gpus
    for s in with_gpus:
        assert s.gpus is not None
        for gpu in s.gpus:
            assert set(gpu) == set(Gpu.model_fields)
            Gpu.model_validate(gpu)

    intersight_gpus = [
        gpu for s in by_collector[ManagerType.INTERSIGHT.value] for gpu in (s.gpus or ())
    ]
    assert intersight_gpus, "some Intersight servers should carry GPUs"
    for gpu in intersight_gpus:
        # Identity is real; every telemetry field is None because the API
        # carries no such field. Reporting zeros would read as a healthy
        # idle GPU. See docs/adr/0017-intersight-collector.md.
        assert gpu["model"]
        assert gpu["memory_bytes"] is None
        assert gpu["temperature_celsius"] is None
        assert gpu["ecc_mode_enabled"] is None
        assert gpu["correctable_error_count"] is None


def test_drive_health_uses_the_health_severity_vocabulary() -> None:
    """Both collectors normalize drive health onto `HealthSeverity` at the
    provider boundary, and `storage.failed_drive_count` counts CRITICAL —
    fixtures speaking any other vocabulary would make the seeded storage
    policy fire in dev and never in production.
    """
    allowed = {s.value for s in HealthSeverity}
    healths = {
        drive["health"]
        for s in generate_servers(seed=42, count=300)
        for drive in s.storage_drives or ()
    }
    assert healths <= allowed
    assert HealthSeverity.CRITICAL.value in healths


def test_only_cisco_servers_have_attachments() -> None:
    servers = list(generate_servers(seed=42, count=300))
    non_cisco_with_attachments = [s for s in servers if s.vendor != "cisco" and s.attachments]
    assert non_cisco_with_attachments == []
    # And at least some Cisco servers do have attachments, at varying counts.
    physical_counts = {
        sum(1 for a in s.attachments if a.interface_kind == "PHYSICAL")
        for s in servers
        if s.vendor == "cisco"
    }
    assert physical_counts & {0, 1, 2, 4}


def test_cisco_attachments_report_both_physical_ports_and_vnics() -> None:
    """The UCS collector reports `adaptorExtEthIf` and `adaptorHostEthIf`
    together, told apart only by `interface_kind`. A fixture with just one
    kind would never catch code that conflates them.
    """
    attached = [s for s in generate_servers(seed=42, count=300) if s.attachments]
    assert attached
    for s in attached:
        kinds = {a.interface_kind for a in s.attachments}
        assert kinds == {"PHYSICAL", "VNIC"}
        assert all(a.provider == ManagerType.UCS_CENTRAL.value for a in s.attachments)


def test_fabric_names_follow_fi_a_b_convention() -> None:
    servers = list(generate_servers(seed=42, count=300))
    for s in servers:
        for attachment in s.attachments:
            assert attachment.fabric in ("A", "B")
            assert attachment.fabric_name is not None
            assert attachment.fabric_name.startswith(f"FI-{attachment.fabric}-")


def test_names_follow_expected_patterns() -> None:
    """Generated names must be in the shapes the real estate uses, because
    they are what `parse_site_code` and the seeded classification rules
    both read. A generator drifting from those shapes would silently make
    every dev/CI fixture classify differently from production.
    """
    servers = list(generate_servers(seed=42, count=300))
    for s in servers:
        assert (
            s.name.startswith("ocp4-hypershift-")
            or s.name.startswith("ocp-")
            or s.name.startswith("ocp4-")
            or s.name.startswith("random-server-")
        ), s.name


def test_every_generated_name_resolves_to_a_site_or_is_deliberately_siteless() -> None:
    servers = list(generate_servers(seed=42, count=300))
    sited = 0
    for s in servers:
        site = parse_site_code(s.name)
        if site is None:
            # Only the deliberate unclassified family has no site token.
            assert s.name.startswith("random-server-"), s.name
        else:
            assert site.value in {m.value for m in SiteCode}
            sited += 1
    # The siteless family is a minority, not the bulk of the fixture.
    assert sited > len(servers) * 0.6


def test_generated_names_cover_every_site() -> None:
    names = [s.name for s in generate_servers(seed=42, count=300)]
    seen = {parse_site_code(n) for n in names} - {None}
    assert seen == set(SiteCode)


def test_list_sites_and_managers_have_unique_names_and_ids() -> None:
    sites = list_sites()
    site_names = [s.name for s in sites]
    site_ids = [s.id for s in sites]
    assert len(site_names) == len(set(site_names))
    assert len(site_ids) == len(set(site_ids))

    managers = list_managers()
    manager_names = [m.name for m in managers]
    manager_ids = [m.id for m in managers]
    assert len(manager_names) == len(set(manager_names))
    assert len(manager_ids) == len(set(manager_ids))


def test_managers_are_exactly_the_implemented_collectors() -> None:
    """Seeding a manager type with no collector would invent a data path
    that cannot exist — and `--manager-type UCS_MANAGER` was removed, so
    there is no Central/Manager pair to model any more.
    """
    assert {m.type for m in list_managers()} == {
        ManagerType.UCS_CENTRAL,
        ManagerType.INTERSIGHT,
        ManagerType.REDFISH_STANDALONE,
    }


def test_manager_ids_match_what_a_real_collector_writes() -> None:
    """`tools.run_collector.manager_for` builds the same id, so a seeded
    fleet and a collected one share one manager document rather than two.
    """
    assert {m.id for m in list_managers()} == {
        manager_for(t, ManagerConnection(endpoint="e", username="u", password="p")).id
        for t in (ManagerType.UCS_CENTRAL, ManagerType.INTERSIGHT, ManagerType.REDFISH_STANDALONE)
    }


def test_every_server_is_owned_by_the_collector_that_could_find_it() -> None:
    """And by exactly one: the two Cisco collectors partition the Cisco
    fleet rather than both claiming it, mirroring the `ManagementMode`
    split the real Intersight collector enforces.
    """
    seen: set[str] = set()
    for s in generate_servers(seed=42, count=300):
        expected = collector_for(s.vendor, s.model or "")
        assert provider_type_for(s) == expected.value
        assert s.manager_id == manager_id_for(expected)
        if s.vendor == "cisco":
            seen.add(expected.value)

    assert seen == {ManagerType.UCS_CENTRAL.value, ManagerType.INTERSIGHT.value}, (
        "Cisco servers should be split across both Cisco collectors"
    )


def test_servers_reference_known_managers() -> None:
    """`ProviderServer` no longer carries a site at all — it is derived
    from the name at ingest — so only the manager reference is checked
    here. Site coverage is asserted by the name tests above.
    """
    manager_ids = {m.id for m in list_managers()}
    servers = list(generate_servers(seed=42, count=300))
    for s in servers:
        assert s.manager_id in manager_ids


def test_site_documents_are_ided_by_their_bare_site_code() -> None:
    """A `Site` document's id must be exactly what `parse_site_code`
    yields, or a server's `site_id` would never join to its site.
    """
    assert {s.id for s in list_sites()} == {m.value for m in SiteCode}


def test_some_servers_have_a_profile_template_and_some_do_not() -> None:
    servers = list(generate_servers(seed=42, count=300))
    with_template = [s for s in servers if s.profile_template_name is not None]
    without_template = [s for s in servers if s.profile_template_name is None]
    assert with_template
    assert without_template


def test_profile_template_name_and_external_id_are_both_set_or_both_none() -> None:
    servers = list(generate_servers(seed=42, count=300))
    for s in servers:
        assert (s.profile_template_name is None) == (s.profile_template_external_id is None)


def test_only_cisco_reports_a_profile_template() -> None:
    """Of the two implemented collectors only UCS reads a profile template:
    a BMC has none, and the OME/OneView collectors that would report their
    own do not exist.
    """
    for s in generate_servers(seed=42, count=300):
        if s.vendor != "cisco":
            assert s.profile_template_name is None
            continue
        if s.profile_template_name is not None:
            # UCS Manager references its Service Profile Template by name.
            assert s.profile_template_external_id == s.profile_template_name
