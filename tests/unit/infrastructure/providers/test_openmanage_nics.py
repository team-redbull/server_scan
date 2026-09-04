"""`..openmanage.mapping.dell_port_nics` — NPAR partitions to physical ports.

Pure function, no I/O: the inputs are exactly what
`..redfish.mapping.nics_from_interfaces` builds from an iDRAC's
`EthernetInterfaces`, with `location` still holding the raw FQDD.

The cases that matter are the ones where a wrong filter loses real
hardware: a card whose identifier does not parse, and a server that never
had partitioning enabled at all.
"""

from __future__ import annotations

from app.domain.ports.provider import ProviderNic
from app.infrastructure.providers.openmanage.mapping import dell_port_nics


def _nic(fqdd: str, *, mac: str = "b0:7b:25:1a:44:c0") -> ProviderNic:
    """
    One interface as the Redfish mapping would have built it.

    Args:
        fqdd (str): The `EthernetInterface.Id`, carried in `location`.
        mac (str): The interface's MAC.

    Returns:
        ProviderNic: The pre-Dell-processing interface. `name` is iDRAC's
            generic label, which is the same on every interface — the whole
            reason the FQDD has to survive to this point.
    """
    return ProviderNic(
        name="System Ethernet Interface",
        mac=mac,
        speed_mbps=25000,
        link_state="UP",
        location=fqdd,
    )


class TestPartitionFiltering:
    """One entry per physical port, not per logical function."""

    def test_a_partitioned_four_port_card_reduces_to_four(self) -> None:
        """The reported case: every port carries 4 partitions, so iDRAC
        reports 16 interfaces for what an operator calls 4 NICs.
        """
        nics = tuple(
            _nic(f"NIC.Integrated.1-{port}-{partition}")
            for port in range(1, 5)
            for partition in range(1, 5)
        )
        assert len(nics) == 16
        kept = dell_port_nics(nics)
        assert [nic.location for nic in kept] == ["1/1/1", "1/2/1", "1/3/1", "1/4/1"]

    def test_an_unpartitioned_server_is_untouched(self) -> None:
        """Partition 1 exists whether or not NPAR is on, so a server
        without it must pass through unchanged rather than being filtered
        by a rule aimed at something it does not have.
        """
        nics = (_nic("NIC.Integrated.1-1-1"), _nic("NIC.Integrated.1-2-1"))
        assert len(dell_port_nics(nics)) == 2

    def test_slot_cards_and_integrated_are_both_recognized(self) -> None:
        """`NIC.Slot.N-...` and `NIC.Integrated.N-...` differ only in kind;
        both carry the same controller-port-partition triple.
        """
        nics = (
            _nic("NIC.Integrated.1-1-1"),
            _nic("NIC.Integrated.1-1-2"),
            _nic("NIC.Slot.2-4-1"),
            _nic("NIC.Slot.2-4-3"),
        )
        assert [nic.location for nic in dell_port_nics(nics)] == ["1/1/1", "2/4/1"]

    def test_ports_keep_the_order_the_bmc_reported(self) -> None:
        """Nothing here sorts. A reordering would make two runs of the same
        unchanged server produce different documents.
        """
        nics = (_nic("NIC.Slot.3-2-1"), _nic("NIC.Integrated.1-1-1"))
        assert [nic.location for nic in dell_port_nics(nics)] == ["3/2/1", "1/1/1"]


class TestWhatIsNeverDropped:
    """The filter may only remove what it positively identified."""

    def test_an_unparseable_identifier_is_kept_as_is(self) -> None:
        """A BMC naming its NICs some other way must not lose them. This is
        the difference between a filter and a data-loss bug.
        """
        nics = (_nic("SomeOtherScheme"), _nic("NIC.Integrated.1-1-1"))
        kept = dell_port_nics(nics)
        assert len(kept) == 2
        assert kept[0].location == "SomeOtherScheme"
        assert kept[0].name == "System Ethernet Interface"

    def test_a_missing_identifier_is_kept_as_is(self) -> None:
        """`location` is None whenever the BMC reported no `Id`."""
        nic = ProviderNic(name="eth0", mac=None, speed_mbps=None, link_state="UNKNOWN")
        assert dell_port_nics((nic,)) == (nic,)

    def test_no_interfaces_stays_empty(self) -> None:
        """An empty tuple is "reported none", and stays that."""
        assert dell_port_nics(()) == ()


class TestNaming:
    """What survives is identifiable."""

    def test_the_fqdd_replaces_idrac_s_generic_label(self) -> None:
        """iDRAC calls every interface "System Ethernet Interface". Keeping
        that would leave four NICs sharing one name and none of them
        identifiable.
        """
        [nic] = dell_port_nics((_nic("NIC.Integrated.1-2-1"),))
        assert nic.name == "NIC.Integrated.1-2-1"
        assert nic.location == "1/2/1"

    def test_the_surviving_partition_keeps_its_own_mac_and_link(self) -> None:
        """Partition 1 is a real interface, not a synthesized summary of
        its port, so its measured fields are carried through untouched.
        """
        nics = (
            _nic("NIC.Integrated.1-1-1", mac="aa:bb:cc:dd:ee:01"),
            _nic("NIC.Integrated.1-1-2", mac="aa:bb:cc:dd:ee:02"),
        )
        [nic] = dell_port_nics(nics)
        assert nic.mac == "aa:bb:cc:dd:ee:01"
        assert nic.speed_mbps == 25000
        assert nic.link_state == "UP"
