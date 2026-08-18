"""Pure `ucsmsdk` managed-object -> `ProviderServer` mapping.

No I/O: `provider.py` makes every XML API call and hands this module plain
MOs to convert. Shared unchanged with the UCS Central collector.

See docs/cisco-collectors.md, "CPU, memory and storage" for the unit
assumptions this module rests on.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.domain.ports.provider import ProviderAttachment, ProviderServer
from app.infrastructure.providers.ucs_common import is_equipped

_BYTES_PER_MB = 1024 * 1024
_NOT_APPLICABLE = "not-applicable"


def _profile_template_fields(
    profile: Any | None,
    *,
    template_dn_by_name: dict[str, str],
) -> tuple[str | None, str | None]:
    """
    Resolve the service-profile template a profile was derived from.

    Args:
        profile (Any | None): The server's `lsServer` service profile, or
            `None` if it has none.
        template_dn_by_name (dict[str, str]): Template name -> DN. Lossy
            across orgs by construction, so it is only a fallback.

    Returns:
        tuple[str | None, str | None]: `(name, external_id)` for
            `ProviderServer.profile_template_*`, or `(None, None)` when the
            server has no profile or its profile came from no template.

    See docs/cisco-collectors.md, "Service profiles and server names".
    """
    if profile is None:
        return None, None
    template_name = getattr(profile, "src_templ_name", None) or None
    if not template_name:
        return None, None
    oper_dn = getattr(profile, "oper_src_templ_name", None) or None
    external_id = oper_dn or template_dn_by_name.get(template_name, template_name)
    return template_name, external_id


def _server_name(server_mo: Any, profile: Any | None) -> str:
    """
    The name an operator would use for this machine.

    Args:
        server_mo (Any): The `computeBlade` or `computeRackUnit` MO.
        profile (Any | None): Its associated `lsServer`, or `None`.

    Returns:
        str: The service profile's name, else the MO's own name, else its
            DN as a last resort.

    See docs/cisco-collectors.md, "Service profiles and server names".
    """
    profile_name = getattr(profile, "name", None) if profile is not None else None
    return profile_name or getattr(server_mo, "name", None) or str(server_mo.dn)


def _management_ip_addr(
    *, profile: Any | None, server_mo: Any, mgmt_ip_by_parent_dn: dict[str, Any]
) -> Any | None:
    """
    Resolve a server's `vnicIpV4PooledAddr`/`vnicIpV4StaticAddr`, trying
    both DNs that MO can legitimately hang directly off of.

    The service profile's own DN is tried first — confirmed against real
    UCS Manager hardware to be the one actually populated — and the
    compute unit's `mgmtController` DN second, which is schema-valid per
    `ucsmsdk`'s `mo_meta.parents` but was empty on the hardware this was
    verified against. See docs/cisco-collectors.md, "BMC and management
    interface selection".

    Args:
        profile (Any | None): The server's `lsServer` service profile, or
            `None`.
        server_mo (Any): The `computeBlade` or `computeRackUnit` MO.
        mgmt_ip_by_parent_dn (dict[str, Any]): From
            `ucs_common.management_ip_by_parent_dn`.

    Returns:
        Any | None: The resolved MO, or `None` if neither DN has one.
    """
    if profile is not None:
        by_profile = mgmt_ip_by_parent_dn.get(profile.dn)
        if by_profile is not None:
            return by_profile
    return mgmt_ip_by_parent_dn.get(f"{server_mo.dn}/mgmt")


def _bmc_address(mgmt_if: Any | None, mgmt_ip_addr: Any | None) -> str | None:
    """
    The CIMC's out-of-band address as a BMC URI.

    Args:
        mgmt_if (Any | None): The server's own `mgmtIf`, or `None`.
        mgmt_ip_addr (Any | None): Its resolved
            `vnicIpV4PooledAddr`/`vnicIpV4StaticAddr`, from
            `_management_ip_addr`, or `None`.

    Returns:
        str | None: An `ipmi://host:623` URI in the form
            `app.domain.value_objects.bmc_address.parse_bmc_address`
            recognizes for Cisco, or `None` when neither source has an
            address.

    See docs/cisco-collectors.md, "BMC and management interface selection".
    """
    addr = getattr(mgmt_ip_addr, "addr", None) if mgmt_ip_addr is not None else None
    if not addr and mgmt_if is not None:
        addr = getattr(mgmt_if, "ext_ip", None)
    if not addr or addr in ("0.0.0.0", "none"):  # noqa: S104 - unset-IP sentinels, not a bind
        return None
    return f"ipmi://{addr}:623"


def _extract_macs(adapter_ifs: list[Any]) -> tuple[str, ...]:
    """
    Pull real MACs off a list of adapter interfaces.

    Args:
        adapter_ifs (list[Any]): `adaptorHostEthIf` or `adaptorExtEthIf`
            MOs.

    Returns:
        tuple[str, ...]: Their MACs in order, skipping UCS's
            "not applicable" and "derived" placeholders.
    """
    macs: list[str] = []
    for mo in adapter_ifs:
        mac = getattr(mo, "mac", None)
        if mac and mac.lower() not in ("not applicable", "derived"):
            macs.append(mac)
    return tuple(macs)


def _nic_macs(*, host_eth_ifs: list[Any], ext_eth_ifs: list[Any]) -> tuple[str, ...]:
    """
    The MACs an OS on this server would report on its own NICs.

    Args:
        host_eth_ifs (list[Any]): `adaptorHostEthIf` (logical vNIC) MOs.
        ext_eth_ifs (list[Any]): `adaptorExtEthIf` (physical port) MOs.

    Returns:
        tuple[str, ...]: vNIC MACs, falling back to physical-port MACs only
            when the server has no vNIC at all.

    See docs/cisco-collectors.md, "Adapter interfaces, MACs and fabric
    attachments".
    """
    host_macs = _extract_macs(host_eth_ifs)
    return host_macs if host_macs else _extract_macs(ext_eth_ifs)


# See docs/cisco-collectors.md, "Adapter interfaces, MACs and fabric
# attachments".
_OPER_STATE_MAP = {
    "operable": "UP",
    "up": "UP",
    "link-up": "UP",
    "admin-down": "DISABLED",
    "disabled": "DISABLED",
    "inoperable": "DOWN",
    "down": "DOWN",
    "link-down": "DOWN",
    "failed": "DOWN",
    "sfp-not-present": "DOWN",
}

_ADMIN_STATE_MAP = {"enabled": "ENABLED", "disabled": "DISABLED"}


def _oper_state(mo: Any) -> str:
    """
    Map UCS's operational-state vocabulary onto the platform's.

    Args:
        mo (Any): Any MO carrying an `oper_state` property.

    Returns:
        str: UP, DOWN, DISABLED, or UNKNOWN for an unrecognized value.
    """
    return _OPER_STATE_MAP.get(str(getattr(mo, "oper_state", "") or "").lower(), "UNKNOWN")


def _admin_state(mo: Any) -> str:
    """
    Map UCS's administrative-state vocabulary onto the platform's.

    Args:
        mo (Any): Any MO carrying an `admin_state` property.

    Returns:
        str: ENABLED, DISABLED, or UNKNOWN for an unrecognized value.
    """
    return _ADMIN_STATE_MAP.get(str(getattr(mo, "admin_state", "") or "").lower(), "UNKNOWN")


def _attachments(
    adapter_ifs: Iterable[Any],
    *,
    provider_type: str,
    interface_kind: str,
    switches_by_id: dict[str, Any],
) -> tuple[ProviderAttachment, ...]:
    """
    Build fabric-interconnect attachments from adapter interfaces.

    Args:
        adapter_ifs (Iterable[Any]): Either every `adaptorExtEthIf` (the
            physical uplinks) or every `adaptorHostEthIf` (the OS-facing
            vNICs) for one server — never both in the same call, so every
            attachment this call produces gets the same `interface_kind`.
        provider_type (str): Which collector observed the attachment, not
            which product owns the fabric.
        interface_kind (str): `"PHYSICAL"` or `"VNIC"`, stamped onto every
            attachment produced from `adapter_ifs`.
        switches_by_id (dict[str, Any]): `networkElement` MOs keyed by
            `id` (`"A"`/`"B"`), from `ucs_manager.provider`'s domain-wide
            query. Supplies each attachment's `fabric_model`/
            `fabric_serial` — the two identifying facts UCS Manager
            actually exposes per Fabric Interconnect; there is no
            separate configured hostname distinct from the domain's own
            shared cluster name in `topSystem.name`. See
            docs/cisco-collectors.md, "Adapter interfaces, MACs and
            fabric attachments".

    Returns:
        tuple[ProviderAttachment, ...]: One attachment per interface with a
            real `switch_id`; interfaces reporting none are skipped.

    See docs/cisco-collectors.md, "Adapter interfaces, MACs and fabric
    attachments".
    """
    attachments: list[ProviderAttachment] = []
    for mo in adapter_ifs:
        switch_id = getattr(mo, "switch_id", None)
        if not switch_id or switch_id.upper() == "NONE":
            continue
        switch = switches_by_id.get(switch_id)
        attachments.append(
            ProviderAttachment(
                type="FABRIC_INTERCONNECT",
                provider=provider_type,
                fabric=switch_id,
                fabric_name=None,
                fabric_id=None,
                fabric_model=getattr(switch, "model", None) if switch is not None else None,
                fabric_serial=getattr(switch, "serial", None) if switch is not None else None,
                server_interface=getattr(mo, "name", None) or getattr(mo, "id", None),
                server_port=None,
                fabric_port=getattr(mo, "peer_dn", None) or None,
                admin_state=_admin_state(mo),
                oper_state=_oper_state(mo),
                speed_mbps=None,
                interface_kind=interface_kind,
            )
        )
    return tuple(attachments)


def _cpu_model(cpu_units: list[Any]) -> str | None:
    """
    The processor model string for the server.

    Args:
        cpu_units (list[Any]): `processorUnit` MOs owned by one server, one
            per socket.

    Returns:
        str | None: The first equipped socket's model, or `None` when no
            socket is equipped or none reports a model.

    See docs/cisco-collectors.md, "CPU, memory and storage".
    """
    for mo in cpu_units:
        if is_equipped(mo):
            model = getattr(mo, "model", None)
            if model:
                return str(model)
    return None


_MEDIA_TYPE_MAP = {"hdd": "HDD", "ssd": "SSD", "nvme": "NVME"}

# `StorageLocalDiskConsts.DISK_STATE_*` mapped onto `HealthSeverity`.
# See docs/cisco-collectors.md, "CPU, memory and storage".
_DISK_HEALTH_MAP = {
    "good": "HEALTHY",
    "online": "HEALTHY",
    "unconfigured-good": "HEALTHY",
    "global-hot-spare": "HEALTHY",
    "dedicated-hot-spare": "HEALTHY",
    "jbod": "HEALTHY",
    "predictive-failure": "WARNING",
    "rebuilding": "WARNING",
    "copyback": "WARNING",
    "foreign-configuration": "WARNING",
    "locked-foreign-configuration": "WARNING",
    "bad": "CRITICAL",
    "failed": "CRITICAL",
    "unconfigured-bad": "CRITICAL",
    "disabled-for-removal": "CRITICAL",
}


def _media_type(mo: Any) -> str:
    """
    Map a disk's `device_type` onto the platform's `MediaType`.

    Args:
        mo (Any): A `storageLocalDisk` MO.

    Returns:
        str: HDD, SSD, NVME, or UNKNOWN.
    """
    return _MEDIA_TYPE_MAP.get(str(getattr(mo, "device_type", "") or "").lower(), "UNKNOWN")


def _disk_health(mo: Any) -> str:
    """
    Map a disk's `disk_state` onto the platform's `HealthSeverity`.

    Args:
        mo (Any): A `storageLocalDisk` MO.

    Returns:
        str: HEALTHY, WARNING, CRITICAL, or UNKNOWN for any state not
            worth distinguishing.
    """
    return _DISK_HEALTH_MAP.get(str(getattr(mo, "disk_state", "") or "").lower(), "UNKNOWN")


def _disk_capacity_bytes(mo: Any) -> int | None:
    """
    One disk's capacity in bytes.

    Args:
        mo (Any): A `storageLocalDisk` MO whose `size` is assumed to be MB.

    Returns:
        int | None: The capacity, or `None` when `size` is absent, the
            `not-applicable` sentinel, or unparseable.

    See docs/cisco-collectors.md, "CPU, memory and storage".
    """
    raw_size = getattr(mo, "size", None)
    if raw_size is None or str(raw_size).lower() == _NOT_APPLICABLE:
        return None
    size_mb = _as_int(raw_size)
    return size_mb * _BYTES_PER_MB if size_mb else None


def _storage_drives(disk_units: list[Any]) -> tuple[tuple[dict[str, object], ...], int]:
    """
    Summarize one server's local disks.

    Args:
        disk_units (list[Any]): `storageLocalDisk` MOs owned by one server.

    Returns:
        tuple[tuple[dict[str, object], ...], int]: `(drives, total_bytes)`
            over every equipped disk. A disk whose capacity could not be
            read still contributes a drive entry, with
            `capacity_bytes=None`, and adds nothing to the total rather
            than counting as zero.

    See docs/cisco-collectors.md, "CPU, memory and storage".
    """
    drives: list[dict[str, object]] = []
    total_bytes = 0
    for mo in disk_units:
        if not is_equipped(mo):
            continue
        capacity_bytes = _disk_capacity_bytes(mo)
        total_bytes += capacity_bytes or 0
        drives.append(
            {
                "id": str(mo.dn),
                "model": getattr(mo, "model", None) or None,
                "serial": getattr(mo, "serial", None) or None,
                "media_type": _media_type(mo),
                "capacity_bytes": capacity_bytes,
                "health": _disk_health(mo),
            }
        )
    return tuple(drives), total_bytes


def compute_unit_to_provider_server(
    server_mo: Any,
    *,
    manager_id: str,
    profile_by_dn: dict[str, Any],
    template_dn_by_name: dict[str, str],
    mgmt_if: Any | None,
    mgmt_ip_by_parent_dn: dict[str, Any],
    ext_eth_ifs: list[Any],
    host_eth_ifs: list[Any],
    cpu_units: list[Any],
    disk_units: list[Any],
    switches_by_id: dict[str, Any],
    provider_type: str = "UCS_MANAGER",
) -> ProviderServer:
    """
    Convert one compute unit and its descendants into a `ProviderServer`.

    Handles `computeBlade` and `computeRackUnit` alike, and serves both
    Cisco collectors unchanged — only `provider_type` differs between them.

    Args:
        server_mo (Any): The `computeBlade` or `computeRackUnit` MO.
        manager_id (str): The manager this server is reported under.
        profile_by_dn (dict[str, Any]): Service profile DN -> `lsServer`.
        template_dn_by_name (dict[str, str]): Template name -> DN.
        mgmt_if (Any | None): The server's own `mgmtIf`, or `None`.
        mgmt_ip_by_parent_dn (dict[str, Any]): Every domain
            `vnicIpV4PooledAddr`/`vnicIpV4StaticAddr` with a real address,
            from `ucs_common.management_ip_by_parent_dn`.
        ext_eth_ifs (list[Any]): Its `adaptorExtEthIf` MOs (physical
            uplinks).
        host_eth_ifs (list[Any]): Its `adaptorHostEthIf` MOs (OS-facing
            vNICs).
        cpu_units (list[Any]): Its `processorUnit` MOs.
        disk_units (list[Any]): Its `storageLocalDisk` MOs.
        switches_by_id (dict[str, Any]): Domain `networkElement` MOs
            keyed by `id` (`"A"`/`"B"`), passed through to `_attachments`.
        provider_type (str): Which collector observed this server.

    Returns:
        ProviderServer: The vendor-neutral DTO the ingest pipeline consumes.

    See docs/cisco-collectors.md, "Shared object model and DN joins".
    """
    profile = profile_by_dn.get(getattr(server_mo, "assigned_to_dn", None) or "")
    template_name, template_external_id = _profile_template_fields(
        profile, template_dn_by_name=template_dn_by_name
    )
    mgmt_ip_addr = _management_ip_addr(
        profile=profile, server_mo=server_mo, mgmt_ip_by_parent_dn=mgmt_ip_by_parent_dn
    )

    total_memory_mb = _as_int(getattr(server_mo, "total_memory", None))
    storage_drives, storage_total_bytes = _storage_drives(disk_units)

    return ProviderServer(
        external_id=server_mo.dn,
        vendor="cisco",
        name=_server_name(server_mo, profile),
        model=getattr(server_mo, "model", None) or None,
        serial=getattr(server_mo, "serial", None) or None,
        system_uuid=getattr(server_mo, "uuid", None) or None,
        nic_macs=_nic_macs(host_eth_ifs=host_eth_ifs, ext_eth_ifs=ext_eth_ifs),
        bmc_address_raw=_bmc_address(mgmt_if, mgmt_ip_addr),
        bmc_mac=getattr(mgmt_if, "mac", None) if mgmt_if is not None else None,
        manager_id=manager_id,
        profile_dn=profile.dn if profile is not None else None,
        profile_template_name=template_name,
        profile_template_external_id=template_external_id,
        cpu_sockets=_as_int(getattr(server_mo, "num_of_cpus", None)),
        cpu_cores=_as_int(getattr(server_mo, "num_of_cores", None)),
        cpu_threads=_as_int(getattr(server_mo, "num_of_threads", None)),
        cpu_model=_cpu_model(cpu_units),
        memory_total_bytes=total_memory_mb * _BYTES_PER_MB,
        storage_total_bytes=storage_total_bytes,
        storage_drives=storage_drives,
        attachments=(
            _attachments(
                ext_eth_ifs,
                provider_type=provider_type,
                interface_kind="PHYSICAL",
                switches_by_id=switches_by_id,
            )
            + _attachments(
                host_eth_ifs,
                provider_type=provider_type,
                interface_kind="VNIC",
                switches_by_id=switches_by_id,
            )
        ),
        tags=(),
    )


def _as_int(value: object) -> int:
    """
    Coerce a UCS numeric attribute to `int`.

    UCS XML attributes arrive as strings and `ucsmsdk` does not coerce
    them, so every numeric field goes through this.

    Args:
        value (object): The raw attribute value, possibly `None`.

    Returns:
        int: The parsed value, or 0 when missing or non-numeric. Never
            raises — one unparseable count must not fail a whole server.
    """
    if value is None:
        return 0
    try:
        return int(str(value))
    except ValueError:
        return 0
