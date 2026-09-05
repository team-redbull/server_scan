"""The FQDD-to-OS-name lookup.

Neither name is derivable from the other, so every case here is about
refusing to guess rather than about clever parsing.
"""

from __future__ import annotations

import pytest

from app.domain.value_objects.nic_names import (
    NicNameCatalog,
    NicNameConfigurationError,
    nic_name_catalog,
)

pytestmark = pytest.mark.unit

_SPEC = "Slot.8=ens8f0np0,ens8f1np1;Integrated.1=eno12399np0,eno12409np1"


def test_a_configured_kind_and_port_resolves() -> None:
    """The whole feature: slot 8 port 1 is the first configured name."""
    catalog = NicNameCatalog.from_spec(_SPEC)

    assert catalog.os_name_for("NIC.Slot.8-1-1") == "ens8f0np0"
    assert catalog.os_name_for("NIC.Slot.8-2-1") == "ens8f1np1"
    assert catalog.os_name_for("NIC.Integrated.1-1-1") == "eno12399np0"


def test_an_npar_partition_does_not_change_the_os_name() -> None:
    """Partition 1 of a partitioned port and an unpartitioned port are the
    same physical interface, so the trailing number is not part of the key.
    """
    catalog = NicNameCatalog.from_spec(_SPEC)

    assert catalog.os_name_for("NIC.Slot.8-1-3") == "ens8f0np0"


@pytest.mark.parametrize(
    "fqdd",
    [
        "NIC.Slot.9-1-1",  # a slot with no configured mapping
        "NIC.Slot.8-9-1",  # a port past the configured names
        "Physical Port 1",  # HPE, which has no FQDD at all
        "",
    ],
)
def test_anything_unconfigured_is_none_rather_than_a_guess(fqdd: str) -> None:
    """A wrong OS name produces a boot configuration that silently does
    not come up, so "not known" has to stay expressible.
    """
    assert NicNameCatalog.from_spec(_SPEC).os_name_for(fqdd) is None


def test_an_empty_spec_maps_nothing() -> None:
    """The shipped default: the UI shows hardware names alone."""
    assert NicNameCatalog.from_spec("").os_name_for("NIC.Slot.8-1-1") is None


@pytest.mark.parametrize("spec", ["Slot.8", "=ens8f0np0", "Slot.8=", "Slot.8=,"])
def test_a_malformed_entry_fails_loudly(spec: str) -> None:
    """At startup, where an operator can still fix it — not per request."""
    with pytest.raises(NicNameConfigurationError):
        NicNameCatalog.from_spec(spec)


def test_the_catalog_is_cached_per_spec() -> None:
    """Built once per unique spec rather than per request."""
    assert nic_name_catalog(_SPEC) is nic_name_catalog(_SPEC)
