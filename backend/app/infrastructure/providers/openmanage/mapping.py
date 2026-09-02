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

from dataclasses import dataclass
from typing import Any


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
