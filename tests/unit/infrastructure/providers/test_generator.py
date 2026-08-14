"""Pure-logic tests for the deterministic fake data generator — no I/O,
no database, safe for `tests/unit`.
"""

from __future__ import annotations

from app.domain.enums import ManagerType, SiteCode
from app.domain.value_objects.site import parse_site_code
from app.infrastructure.providers.fake.generator import (
    generate_servers,
    list_managers,
    list_sites,
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
    assert vendors <= {"dell", "cisco", "hp"}


def test_bmc_address_forms_match_vendor() -> None:
    servers = list(generate_servers(seed=42, count=300))
    for s in servers:
        assert s.bmc_address_raw is not None
        if s.vendor == "dell":
            assert s.bmc_address_raw.startswith("idrac-virtualmedia://")
            assert s.bmc_address_raw.endswith("/redfish/v1/Systems/System.Embedded.1")
        elif s.vendor == "hp":
            assert s.bmc_address_raw.startswith("redfish-virtualmedia://")
            assert s.bmc_address_raw.endswith("/redfish/v1/Systems/1")
        elif s.vendor == "cisco":
            assert s.bmc_address_raw.startswith("ipmi://")
            assert s.bmc_address_raw.endswith(":623")


def test_only_cisco_servers_have_attachments() -> None:
    servers = list(generate_servers(seed=42, count=300))
    non_cisco_with_attachments = [s for s in servers if s.vendor != "cisco" and s.attachments]
    assert non_cisco_with_attachments == []
    # And at least some Cisco servers do have attachments, at varying counts.
    cisco_attachment_counts = {len(s.attachments) for s in servers if s.vendor == "cisco"}
    assert cisco_attachment_counts & {0, 1, 2, 4}


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


def test_at_least_one_ucs_central_ucs_manager_pair() -> None:
    managers = list_managers()
    centrals = [m for m in managers if m.type == ManagerType.UCS_CENTRAL]
    assert centrals
    central_ids = {m.id for m in centrals}
    children = [m for m in managers if m.type == ManagerType.UCS_MANAGER]
    assert children
    assert any(m.parent_manager_id in central_ids for m in children)


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


def test_profile_template_external_id_form_matches_vendor() -> None:
    servers = list(generate_servers(seed=42, count=300))
    for s in servers:
        if s.profile_template_name is None:
            continue
        if s.vendor == "cisco":
            # UCS Manager references its Service Profile Template by name.
            assert s.profile_template_external_id == s.profile_template_name
        elif s.vendor == "hp":
            assert s.profile_template_external_id is not None
            assert s.profile_template_external_id.startswith("/rest/server-profile-templates/")
        elif s.vendor == "dell":
            assert s.profile_template_external_id is not None
            assert s.profile_template_external_id.isdigit()
