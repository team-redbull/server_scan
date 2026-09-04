"""Pure OME JSON -> identity mapping.

No I/O: `provider.py` makes every REST call and hands this module plain
dicts to convert. The OME field names here (`ProfileName`, `TargetName`,
`DeviceServiceTag`) are validated facts carried over from a production Dell
scanner — see docs/dell-collectors.md for the provenance.

This module maps **identity only**. Hardware detail comes from each
server's iDRAC over Redfish, not from OME's `InventoryDetails` — see
docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md. The CPU,
memory, storage and NIC mappers that used to live here were deleted with
that change rather than left unreachable; they rested on heuristics (disk
capacity parsed out of the model string, threads as `2 x cores`) that
existed only because OME did not report the real values, and Redfish does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from app.domain.ports.provider import ProviderNic

# Dell's FQDD for a network interface, as iDRAC reports it in
# `EthernetInterface.Id`: a kind, then controller-port-partition.
#
#     NIC.Integrated.1-1-1   integrated controller 1, port 1, partition 1
#     NIC.Slot.2-4-3         card in slot 2, port 4, partition 3
#
# The three numbers are what an operator reads as a NIC's location, and
# the partition is what NPAR multiplies: a partitioned 4-port card reports
# 16 interfaces, four per physical port, each with its own MAC.
_FQDD_RE = re.compile(r"^NIC\.[A-Za-z]+\.(\d+)-(\d+)-(\d+)$")


def _opt_str(value: object) -> str | None:
    """
    Normalize an OME string field to a non-empty `str` or `None`.

    Args:
        value (object): A raw OME field value.

    Returns:
        str | None: The stripped string, or `None` when missing or blank.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def idrac_bmc_address(idrac_ip: str | None) -> str | None:
    """
    Render a server's iDRAC IP as the Dell BMC URI the platform expects.

    Kept, and deliberately preferred over the `https://<host>` form the
    Redfish collector reports for a standalone BMC: this is the exact shape
    `app.domain.value_objects.bmc_address.parse_bmc_address` documents for
    Dell, and what a Metal3 `BareMetalHost` round-trips into
    `spec.bmc.address`. Collecting hardware over Redfish must not silently
    downgrade a Dell server's stored BMC address.

    Args:
        idrac_ip (str | None): The iDRAC address OME reports as the
            profile's `TargetName` / the device's `DeviceName`.

    Returns:
        str | None: An `idrac-virtualmedia://<ip>/redfish/v1/Systems/
            System.Embedded.1` URI, or `None` when no address is known.
    """
    ip = _opt_str(idrac_ip)
    if not ip:
        return None
    return f"idrac-virtualmedia://{ip}/redfish/v1/Systems/System.Embedded.1"


def dell_port_nics(nics: tuple[ProviderNic, ...]) -> tuple[ProviderNic, ...]:
    """
    Reduce iDRAC's partition-level NICs to one entry per physical port.

    NIC partitioning (NPAR) splits each physical port into up to four
    logical functions, and iDRAC reports every one as its own
    `EthernetInterface` with its own MAC. A 4-port partitioned card is
    therefore 16 interfaces, which describes the server's *logical*
    plumbing rather than what is cabled — an operator asking "how many
    NICs does this box have" means four.

    Keeping the **first** partition of each port, rather than merging them,
    is deliberate: partition 1 is the one that exists on every Dell NIC
    whether NPAR is enabled or not, so an unpartitioned server is
    unaffected by this function, and the surviving entry is a real
    interface with a real MAC rather than a synthesized summary.

    An interface whose identifier is not a recognizable FQDD is kept
    untouched. A BMC that names its NICs some other way must not have them
    silently dropped — this filter can only ever remove something it
    positively identified as a non-first partition.

    Args:
        nics (tuple[ProviderNic, ...]): Every interface the BMC reported,
            as `..redfish.mapping.nics_from_interfaces` built them, with
            `location` still holding the raw FQDD.

    Returns:
        tuple[ProviderNic, ...]: One entry per physical port, each named by
            its FQDD and located as `controller/port/partition` (`1/1/1`),
            plus any interface whose identifier could not be parsed, all in
            the order the BMC reported them.
    """
    kept: list[ProviderNic] = []
    for nic in nics:
        match = _FQDD_RE.match(nic.location or "")
        if match is None:
            kept.append(nic)
            continue
        controller, port, partition = match.groups()
        if int(partition) != 1:
            continue
        kept.append(
            replace(
                nic,
                # The FQDD, not iDRAC's "System Ethernet Interface", which
                # is the same string on every interface and so names none
                # of them.
                name=nic.location or nic.name,
                location=f"{controller}/{port}/{partition}",
            )
        )
    return tuple(kept)


@dataclass(frozen=True, slots=True)
class OmeIdentity:
    """
    What OME alone knows about one Dell server.

    Everything here is unavailable from the server's own BMC: a service
    profile and its deployment template are OME constructs, and the name is
    the operator's, not anything iDRAC reports. This is the half of a
    collected Dell server that Redfish cannot supply.

    Attributes:
        name (str): The profile name, and the server's name. Site parsing
            and classification both key off it.
        idrac_ip (str | None): The BMC address to collect hardware from.
        serial (str | None): The Dell service tag, from the managed device.
        model (str | None): The device model, when OME has a managed device.
        profile_template_name (str | None): The OME deployment template
            (SPT) this profile was created from.
        profile_template_external_id (str | None): That template's OME id.
        bmc_address_raw (str | None): The Metal3-shaped BMC URI.
    """

    name: str
    idrac_ip: str | None
    serial: str | None
    model: str | None
    profile_template_name: str | None
    profile_template_external_id: str | None
    bmc_address_raw: str | None


def identity_from_profile(*, profile: dict[str, Any], device: dict[str, Any]) -> OmeIdentity:
    """
    Build one server's identity from its OME profile and managed device.

    Args:
        profile (dict[str, Any]): One `/ProfileService/Profiles` entry;
            `ProfileName` is the server name, `TargetName` its iDRAC IP, and
            `TemplateName`/`TemplateId` the deployment template it came from.
        device (dict[str, Any]): The `/DeviceService/Devices` entry joined by
            iDRAC IP, or `{}` when OME has no managed device for the profile.
            `Model` and `DeviceServiceTag` come from here.

    Returns:
        OmeIdentity: The identity half of a collected Dell server. The site
            is intentionally absent — it is parsed from the name downstream.
    """
    name = _opt_str(profile.get("ProfileName")) or ""
    idrac_ip = _opt_str(profile.get("TargetName")) or _opt_str(device.get("DeviceName"))
    return OmeIdentity(
        name=name,
        idrac_ip=idrac_ip,
        serial=_opt_str(device.get("DeviceServiceTag")),
        model=_opt_str(device.get("Model")),
        profile_template_name=_opt_str(profile.get("TemplateName")),
        profile_template_external_id=_opt_str(profile.get("TemplateId")),
        bmc_address_raw=idrac_bmc_address(idrac_ip),
    )
