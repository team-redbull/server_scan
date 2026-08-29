"""Cisco Intersight managed objects -> `ProviderServer`.

Pure functions over the decoded JSON the REST API returns, with no
client and no I/O, so every rule here is testable against a recorded
payload. See docs/adr/0017-intersight-collector.md and
docs/cisco-collectors.md, "Intersight managed objects".

Two rules from the port contract are load-bearing throughout:
a field this collector could not read is `None`, never `0` or `()`; and
`"PHYSICAL"` vs `"VNIC"` on an attachment is a real distinction the
platform counts on, not a label.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.domain.ports.provider import ProviderAttachment, ProviderServer
from app.infrastructure.providers.ucs_common import (
    normalize_admin_state,
    normalize_oper_state,
)

# Cisco reports `TotalMemory` and a disk's `Size` in "MB" and means
# 2**20 bytes, matching what `..ucs_manager.mapping` already assumes for
# the same hardware. `TotalMemory` carries no documented unit at all —
# see ADR-0017's UNVERIFIED list, which is why a disk's byte-denominated
# `NonCoercedSizeBytes` is preferred over its `Size` wherever present.
_BYTES_PER_MB = 1024 * 1024

# `ManagementMode` values a server can report. `UCSM` is the set the UCS
# Central collector already owns; see ADR-0017, "Decision 3".
MODE_UCSM = "UCSM"
MODE_IMM = "Intersight"
MODE_STANDALONE = "IntersightStandalone"

_UNSET_ADDRESSES = frozenset({"0.0.0.0", "none", "::"})  # noqa: S104 - sentinels, not a bind


def _text(value: object) -> str | None:
    """
    A non-empty trimmed string, or None.

    Args:
        value (object): Any reported value.

    Returns:
        str | None: The trimmed text, or None when absent or blank.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: object) -> int | None:
    """
    An integer, or None when the value is absent or not numeric.

    Intersight reports several counts and sizes as strings, so this
    accepts either form rather than assuming the JSON type.

    Args:
        value (object): Any reported value.

    Returns:
        int | None: The integer, or None.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def moref(value: object) -> str | None:
    """
    The `Moid` a relationship field points at.

    Intersight expresses every parent/child link as an embedded
    `mo.MoRef` object rather than a bare id, and reports an unset
    relationship as `null`. This is the join key the whole fleet-wide
    request plan turns on — see ADR-0017, "The request plan".

    Args:
        value (object): A relationship field's value.

    Returns:
        str | None: The referenced `Moid`, or None when unset.
    """
    if isinstance(value, Mapping):
        return _text(value.get("Moid"))
    return None


def management_mode(summary: Mapping[str, Any]) -> str:
    """
    Which product actually manages this server.

    Args:
        summary (Mapping[str, Any]): A `compute.PhysicalSummary`.

    Returns:
        str: `ManagementMode`, defaulting to `IntersightStandalone` — the
            same default the API's own schema declares for the field.
    """
    return _text(summary.get("ManagementMode")) or MODE_STANDALONE


def server_name(summary: Mapping[str, Any], profile: Mapping[str, Any] | None) -> str | None:
    """
    The name an operator would recognise this server by.

    The trap ADR-0009 paid for on UCS Manager, in Intersight's own form.
    `compute.PhysicalSummary.Name` is documented as never being an
    operator hostname — it is the fabric-interconnect cluster name plus a
    chassis/slot when UCSM-attached, the CIMC's own name in standalone
    mode, and model plus chassis/server id under Intersight management.
    Since the platform parses a server's *site* out of this name and
    filters the fleet on it, sourcing it wrong collects nothing rather
    than failing.

    Args:
        summary (Mapping[str, Any]): A `compute.PhysicalSummary`.
        profile (Mapping[str, Any] | None): Its associated
            `server.Profile`, when it has one.

    Returns:
        str | None: The server profile's name where one is assigned, then
            the operator's own label, then whatever the summary calls it.
    """
    if profile is not None:
        name = _text(profile.get("Name"))
        if name:
            return name
    return _text(summary.get("UserLabel")) or _text(summary.get("Name"))


def bmc_address(summary: Mapping[str, Any], interface: Mapping[str, Any] | None) -> str | None:
    """
    The CIMC's out-of-band address, as a BMC URI.

    Args:
        summary (Mapping[str, Any]): A `compute.PhysicalSummary`, whose
            `MgmtIpAddress` needs no extra query.
        interface (Mapping[str, Any] | None): The server's
            `management.Interface`, used only when the summary has none.

    Returns:
        str | None: `ipmi://host:623`, the form
            `app.domain.value_objects.bmc_address.parse_bmc_address`
            already recognises for Cisco, or None.
    """
    address = _text(summary.get("MgmtIpAddress"))
    if not address and interface is not None:
        address = _text(interface.get("IpAddress")) or _text(interface.get("Ipv4Address"))
    if not address or address.lower() in _UNSET_ADDRESSES:
        return None
    return f"ipmi://{address}:623"


def memory_total_bytes(summary: Mapping[str, Any]) -> int | None:
    """
    Installed memory, in bytes.

    Args:
        summary (Mapping[str, Any]): A `compute.PhysicalSummary`.

    Returns:
        int | None: The total, or None when unreported. The `TotalMemory`
            unit is undocumented — see ADR-0017's UNVERIFIED list, item 1.
    """
    total = _as_int(summary.get("TotalMemory"))
    return total * _BYTES_PER_MB if total else None


def drive(disk: Mapping[str, Any]) -> dict[str, object]:
    """
    One `storage.PhysicalDisk` as the platform's drive shape.

    Args:
        disk (Mapping[str, Any]): A `storage.PhysicalDisk`.

    Returns:
        dict[str, object]: Keys mirroring
            `app.domain.models.hardware.Drive`.
    """
    return {
        "id": _text(disk.get("DiskId")) or _text(disk.get("Moid")),
        "model": _text(disk.get("Model")) or _text(disk.get("Pid")),
        "serial": _text(disk.get("Serial")),
        "media_type": _text(disk.get("Type")),
        "capacity_bytes": _capacity_bytes(disk),
        "health": _drive_health(disk),
    }


def _capacity_bytes(disk: Mapping[str, Any]) -> int | None:
    """
    A drive's capacity in bytes.

    `NonCoercedSizeBytes` is preferred because it is denominated in bytes
    by its own name, leaving nothing to assume; `Size` is a string
    documented as MB and is the fallback.

    Args:
        disk (Mapping[str, Any]): A `storage.PhysicalDisk`.

    Returns:
        int | None: The capacity, or None when neither field is present.
    """
    exact = _as_int(disk.get("NonCoercedSizeBytes"))
    if exact:
        return exact
    size_mb = _as_int(disk.get("Size"))
    return size_mb * _BYTES_PER_MB if size_mb else None


def _drive_health(disk: Mapping[str, Any]) -> str:
    """
    A drive's health in the platform's vocabulary.

    `Health` is the field Intersight surfaces in its own UI; `DriveState`
    is the controller's view and is consulted only when `Health` is
    absent, since a predicted failure there is still worth a warning.

    Args:
        disk (Mapping[str, Any]): A `storage.PhysicalDisk`.

    Returns:
        str: HEALTHY, WARNING, CRITICAL or UNKNOWN.
    """
    raw = (_text(disk.get("Health")) or _text(disk.get("DriveState")) or "").lower()
    if raw in {"good", "healthy", "online", "optimal", "jbod", "unconfigured good"}:
        return "HEALTHY"
    if raw in {"warning", "degraded", "predictive-failure", "predicted-failure", "rebuilding"}:
        return "WARNING"
    if raw in {"critical", "bad", "failed", "offline", "unconfigured bad", "foreign"}:
        return "CRITICAL"
    if str(disk.get("FailurePredicted")).lower() == "true":
        return "WARNING"
    return "UNKNOWN"


def gpu(card: Mapping[str, Any]) -> dict[str, object]:
    """
    One `graphics.Card` as the platform's GPU shape.

    Every telemetry field is `None` by construction rather than by
    failure: this API version carries no GPU memory, temperature, power
    or ECC field at all. That is a capability ceiling recorded in
    ADR-0017, "Decision 5", not a gap to fill in later.

    Args:
        card (Mapping[str, Any]): A `graphics.Card`.

    Returns:
        dict[str, object]: Keys mirroring `app.domain.models.hardware.Gpu`.
    """
    return {
        "model": _text(card.get("Model")) or _text(card.get("Pid")),
        "vendor": _text(card.get("Vendor")),
        "serial": _text(card.get("Serial")),
        "memory_bytes": None,
        "memory_type": None,
        "ecc_mode_enabled": None,
        "correctable_error_count": None,
        "uncorrectable_error_count": None,
        "temperature_celsius": None,
        "power_watts": None,
        "health": normalize_oper_state(card.get("OperState")),
    }


def attachment(
    interface: Mapping[str, Any], *, provider_type: str, interface_kind: str
) -> ProviderAttachment:
    """
    One adapter interface as a fabric attachment.

    Args:
        interface (Mapping[str, Any]): An `adapter.ExtEthInterface` (a
            cabled uplink) or an `adapter.HostEthInterface` (an OS-facing
            vNIC carved out of one).
        provider_type (str): The collector that observed it.
        interface_kind (str): `"PHYSICAL"` or `"VNIC"`.

    Returns:
        ProviderAttachment: The attachment. `speed_mbps` is always None —
            neither interface class carries a numeric speed, and the
            switch-side ports report a free-form string this collector
            deliberately does not guess at (ADR-0017, "Decision 5").
    """
    return ProviderAttachment(
        type="FABRIC_INTERCONNECT",
        provider=provider_type,
        fabric=_text(interface.get("SwitchId")),
        fabric_name=None,
        fabric_id=None,
        fabric_model=None,
        fabric_serial=None,
        server_interface=(
            _text(interface.get("Name"))
            or _text(interface.get("ExtEthInterfaceId"))
            or _text(interface.get("HostEthInterfaceId"))
        ),
        server_port=None,
        fabric_port=_text(interface.get("PeerDn")) or _text(interface.get("PeerPortId")),
        admin_state=normalize_admin_state(interface.get("AdminState")),
        oper_state=normalize_oper_state(interface.get("OperState")),
        speed_mbps=None,
        interface_kind=interface_kind,
    )


def _macs(interfaces: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """
    Every MAC these interfaces report, in order, without duplicates.

    Args:
        interfaces (Iterable[Mapping[str, Any]]): Adapter interfaces.

    Returns:
        tuple[str, ...]: Normalized MACs.
    """
    seen: dict[str, None] = {}
    for interface in interfaces:
        mac = _text(interface.get("MacAddress"))
        if mac:
            seen.setdefault(mac.lower(), None)
    return tuple(seen)


def to_provider_server(
    summary: Mapping[str, Any],
    *,
    provider_type: str,
    manager_id: str | None,
    profile: Mapping[str, Any] | None = None,
    template: Mapping[str, Any] | None = None,
    ext_interfaces: list[Mapping[str, Any]] | None = None,
    host_interfaces: list[Mapping[str, Any]] | None = None,
    disks: list[Mapping[str, Any]] | None = None,
    cards: list[Mapping[str, Any]] | None = None,
    management_interface: Mapping[str, Any] | None = None,
) -> ProviderServer:
    """
    Assemble one server from its summary and everything joined to it.

    Every sub-resource argument distinguishes "not queried this run"
    (`None`) from "queried, and this server has none" (`[]`), because
    the two mean different things downstream: `IngestService` carries the
    stored value forward for a `None` and overwrites for a real value.
    Collapsing them is what once wrote zero drives over a real inventory
    and reported a failed disk as recovered.

    Args:
        summary (Mapping[str, Any]): The `compute.PhysicalSummary` anchor.
        provider_type (str): The collector's `ManagerType` value.
        manager_id (str | None): The `Manager` projection's id.
        profile (Mapping[str, Any] | None): Its `server.Profile`. Used
            for the name and the template only: unlike UCS Manager's
            `lsServer`, a `server.Profile` has no `Dn` at all, so the
            only DN available is `ServiceProfile` on a UCSM-mode summary.
        template (Mapping[str, Any] | None): The profile's source
            `server.ProfileTemplate`.
        ext_interfaces (list[Mapping[str, Any]] | None):
            `adapter.ExtEthInterface` MOs, the physical uplinks.
        host_interfaces (list[Mapping[str, Any]] | None):
            `adapter.HostEthInterface` MOs, the vNICs.
        disks (list[Mapping[str, Any]] | None): `storage.PhysicalDisk` MOs.
        cards (list[Mapping[str, Any]] | None): `graphics.Card` MOs.
        management_interface (Mapping[str, Any] | None): The BMC's
            `management.Interface`.

    Returns:
        ProviderServer: The normalized server.
    """
    ext = list(ext_interfaces) if ext_interfaces is not None else None
    host = list(host_interfaces) if host_interfaces is not None else None

    macs: tuple[str, ...] | None = None
    if host is not None or ext is not None:
        # vNIC MACs are what an OS reports; the physical ports' own MACs
        # stand in only for a server that has no vNIC at all.
        macs = _macs(host or []) or _macs(ext or [])

    attachments: list[ProviderAttachment] = []
    for interface in ext or ():
        # An uplink reporting no fabric is not cabled to one. Skipped
        # rather than emitted with a null fabric, matching UCS Manager.
        if _text(interface.get("SwitchId")):
            attachments.append(
                attachment(interface, provider_type=provider_type, interface_kind="PHYSICAL")
            )
    for interface in host or ():
        attachments.append(
            attachment(interface, provider_type=provider_type, interface_kind="VNIC")
        )

    drives = [drive(disk) for disk in disks] if disks is not None else None
    storage_total: int | None = None
    if drives is not None:
        # A drive whose capacity could not be read contributes nothing
        # rather than zero — the total is still the best figure available,
        # and `None` here would discard the drives that did report.
        measured = [d["capacity_bytes"] for d in drives]
        storage_total = sum(c for c in measured if isinstance(c, int)) or None

    return ProviderServer(
        external_id=external_id(summary),
        vendor="cisco",
        name=server_name(summary, profile) or "",
        model=_text(summary.get("Model")),
        serial=_text(summary.get("Serial")),
        system_uuid=_text(summary.get("Uuid")),
        nic_macs=macs,
        bmc_address_raw=bmc_address(summary, management_interface),
        bmc_mac=_text(management_interface.get("MacAddress")) if management_interface else None,
        manager_id=manager_id,
        profile_dn=_text(summary.get("ServiceProfile")),
        profile_template_name=_text(template.get("Name")) if template else None,
        profile_template_external_id=_text(template.get("Moid")) if template else None,
        cpu_sockets=_as_int(summary.get("NumCpus")),
        cpu_cores=_as_int(summary.get("NumCpuCores")),
        cpu_threads=_as_int(summary.get("NumThreads")),
        cpu_model=None,
        memory_total_bytes=memory_total_bytes(summary),
        storage_total_bytes=storage_total,
        storage_drives=tuple(drives) if drives is not None else None,
        gpus=tuple(gpu(card) for card in cards) if cards is not None else None,
        attachments=tuple(attachments),
    )


def external_id(summary: Mapping[str, Any]) -> str:
    """
    A stable identity for one server.

    `Moid` rather than `Dn`: it is unique across the whole tenant and is
    already the join key every sub-resource references. Prefixed so it
    reads unambiguously beside a UCS Central `compute/sys-1009/...` DN.

    Args:
        summary (Mapping[str, Any]): A `compute.PhysicalSummary`.

    Returns:
        str: `intersight/<Moid>`, falling back to the DN if a row somehow
            carries no `Moid`.
    """
    identity = _text(summary.get("Moid")) or _text(summary.get("Dn")) or ""
    return f"intersight/{identity}"
