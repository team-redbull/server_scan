"""`app.infrastructure.providers.ucs_manager.mapping` — pure MO ->
`ProviderServer` conversion, tested with plain `SimpleNamespace` stand-ins
for `ucsmsdk` managed objects (attribute names match the real MO classes'
`prop_meta`, confirmed against the installed `ucsmsdk==0.9.27` package —
see `mapping.py`'s module docstring) rather than constructing real
`ucsmsdk` MO instances, which need a live/mocked XML response to build.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.infrastructure.providers.ucs_manager.mapping import compute_unit_to_provider_server

pytestmark = pytest.mark.unit


def _blade(**overrides: object) -> SimpleNamespace:
    defaults = {
        "dn": "sys/chassis-1/blade-3",
        "name": "blade-3",
        "model": "UCSB-B200-M6",
        "vendor": "Cisco Systems Inc",
        "serial": "FCH12345678",
        "uuid": "11111111-2222-3333-4444-555555555555",
        "num_of_cpus": "2",
        "num_of_cores": "32",
        "num_of_threads": "64",
        "total_memory": "524288",  # MB
        "available_memory": "524288",
        "presence": "equipped",
        "oper_state": "ok",
        "assigned_to_dn": "org-root/ls-worker-01",
        "server_id": "1/3",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _profile(**overrides: object) -> SimpleNamespace:
    defaults = {"dn": "org-root/ls-worker-01", "src_templ_name": "worker-template"}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _template(**overrides: object) -> SimpleNamespace:
    defaults = {"name": "worker-template", "dn": "org-root/ls-template-worker-template"}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _mgmt_if(**overrides: object) -> SimpleNamespace:
    defaults = {"access": "out-of-band", "ext_ip": "10.1.2.3", "mac": "00:11:22:33:44:55"}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _adapter_if(**overrides: object) -> SimpleNamespace:
    defaults = {
        "name": "eth0",
        "id": "1",
        "mac": "AA:BB:CC:DD:EE:01",
        "switch_id": "A",
        "admin_state": "enabled",
        "oper_state": "up",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestComputeUnitToProviderServer:
    def test_full_mapping(self) -> None:
        blade = _blade()
        profile = _profile()
        template = _template()
        mgmt_if = _mgmt_if()
        adapter_if = _adapter_if()

        result = compute_unit_to_provider_server(
            blade,
            manager_id="mgr_ucsm_dc1",
            site_id="site_dc1",
            profile_by_dn={profile.dn: profile},
            template_dn_by_name={template.name: template.dn},
            mgmt_if=mgmt_if,
            adapter_ifs=[adapter_if],
        )

        assert result.external_id == "sys/chassis-1/blade-3"
        assert result.vendor == "cisco"
        assert result.name == "blade-3"
        assert result.model == "UCSB-B200-M6"
        assert result.serial == "FCH12345678"
        assert result.system_uuid == "11111111-2222-3333-4444-555555555555"
        assert result.manager_id == "mgr_ucsm_dc1"
        assert result.site_id == "site_dc1"
        assert result.profile_template_name == "worker-template"
        assert result.profile_template_external_id == "org-root/ls-template-worker-template"
        assert result.cpu_sockets == 2
        assert result.cpu_cores == 32
        assert result.cpu_threads == 64
        assert result.memory_total_bytes == 524288 * 1024 * 1024
        assert result.bmc_address_raw == "ipmi://10.1.2.3:623"
        assert result.bmc_mac == "00:11:22:33:44:55"
        assert result.nic_macs == ("AA:BB:CC:DD:EE:01",)
        assert len(result.attachments) == 1
        assert result.attachments[0].fabric == "A"
        assert result.attachments[0].server_interface == "eth0"

    def test_no_assigned_profile_leaves_template_fields_none(self) -> None:
        blade = _blade(assigned_to_dn="")
        result = compute_unit_to_provider_server(
            blade,
            manager_id="mgr_1",
            site_id=None,
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            adapter_ifs=[],
        )
        assert result.profile_template_name is None
        assert result.profile_template_external_id is None

    def test_profile_without_a_template_leaves_template_fields_none(self) -> None:
        blade = _blade()
        profile = _profile(src_templ_name="")
        result = compute_unit_to_provider_server(
            blade,
            manager_id="mgr_1",
            site_id=None,
            profile_by_dn={profile.dn: profile},
            template_dn_by_name={},
            mgmt_if=None,
            adapter_ifs=[],
        )
        assert result.profile_template_name is None
        assert result.profile_template_external_id is None

    def test_template_name_with_no_matching_template_falls_back_to_name(self) -> None:
        blade = _blade()
        profile = _profile()
        result = compute_unit_to_provider_server(
            blade,
            manager_id="mgr_1",
            site_id=None,
            profile_by_dn={profile.dn: profile},
            template_dn_by_name={},  # no lsServiceProfileTemplate matched
            mgmt_if=None,
            adapter_ifs=[],
        )
        assert result.profile_template_name == "worker-template"
        assert result.profile_template_external_id == "worker-template"

    def test_no_mgmt_if_means_no_bmc_address(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            site_id=None,
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            adapter_ifs=[],
        )
        assert result.bmc_address_raw is None
        assert result.bmc_mac is None

    @pytest.mark.parametrize("unset_ip", ["0.0.0.0", "none", "", None])  # noqa: S104 - sentinel value, not a bind
    def test_unset_cimc_ip_means_no_bmc_address(self, unset_ip: str | None) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            site_id=None,
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=_mgmt_if(ext_ip=unset_ip),
            adapter_ifs=[],
        )
        assert result.bmc_address_raw is None

    def test_adapter_with_no_switch_id_is_not_an_attachment(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            site_id=None,
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            adapter_ifs=[_adapter_if(switch_id="NONE")],
        )
        assert result.attachments == ()
        # The NIC's MAC is still real and still reported, even though it
        # isn't attached to a fabric — those are independent facts.
        assert result.nic_macs == ("AA:BB:CC:DD:EE:01",)

    def test_multiple_adapters_preserve_order(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            site_id=None,
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            adapter_ifs=[
                _adapter_if(name="eth0", mac="AA:00:00:00:00:00", switch_id="A"),
                _adapter_if(name="eth1", mac="AA:00:00:00:00:01", switch_id="B"),
            ],
        )
        assert result.nic_macs == ("AA:00:00:00:00:00", "AA:00:00:00:00:01")
        assert [a.fabric for a in result.attachments] == ["A", "B"]

    @pytest.mark.parametrize(
        ("raw", "expected"), [("32", 32), (None, 0), ("not-a-number", 0), ("", 0)]
    )
    def test_numeric_fields_parse_defensively(self, raw: str | None, expected: int) -> None:
        result = compute_unit_to_provider_server(
            _blade(num_of_cpus=raw),
            manager_id="mgr_1",
            site_id=None,
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            adapter_ifs=[],
        )
        assert result.cpu_sockets == expected

    def test_rack_unit_shape_maps_the_same_way(self) -> None:
        # computeRackUnit carries the same relevant attribute set as
        # computeBlade (see mapping.py's module docstring) — no
        # chassis_id/slot_id needed for identity, `dn` alone is enough.
        rack_unit = SimpleNamespace(
            dn="sys/rack-unit-1",
            name="rack-unit-1",
            model="UCSC-C220-M6",
            vendor="Cisco Systems Inc",
            serial="WZP99999999",
            uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            num_of_cpus="1",
            num_of_cores="16",
            num_of_threads="32",
            total_memory="131072",
            presence="equipped",
            oper_state="ok",
            assigned_to_dn="",
        )
        result = compute_unit_to_provider_server(
            rack_unit,
            manager_id="mgr_1",
            site_id="site_dc2",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            adapter_ifs=[],
        )
        assert result.external_id == "sys/rack-unit-1"
        assert result.serial == "WZP99999999"
        assert result.cpu_sockets == 1
