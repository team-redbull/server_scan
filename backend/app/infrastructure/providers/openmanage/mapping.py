"""Pure OME JSON -> `ProviderServer` mapping.

No I/O: `provider.py` makes every REST call and hands this module plain
dicts to convert. The OME field names here (`ProfileName`, `TargetName`,
`DeviceServiceTag`, the `InventoryInfo` shapes) are validated facts carried
over from a production Dell scanner — see docs/dell-collectors.md for the
provenance and the unit assumptions this module rests on.
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.ports.provider import ProviderNic, ProviderServer

_BYTES_PER_MB = 1024 * 1024
_VENDOR = "dell"

# Binary size units, for the `serverArrayDisks` `Size` field, which OME has
# reported both as a plain byte count and as a "<number> <unit>" string.
# See docs/dell-collectors.md, "Capacity units".
_UNIT_BYTES = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
    "PB": 1024**5,
}
_SIZE_RE = re.compile(r"^([\d.]+)\s*([A-Za-z]+)?$")


def _as_int(value: object) -> int:
    """
    Coerce an OME numeric field to `int`, treating anything unparseable as
    zero.

    Args:
        value (object): A raw OME field value.

    Returns:
        int: The parsed integer, or `0` when the value is missing or not a
            number.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


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

    Args:
        idrac_ip (str | None): The iDRAC address OME reports as the
            profile's `TargetName` / the device's `DeviceName`.

    Returns:
        str | None: An `idrac-virtualmedia://<ip>/redfish/v1/Systems/
            System.Embedded.1` URI — the exact form
            `app.domain.value_objects.bmc_address.parse_bmc_address`
            documents for Dell and Metal3 round-trips into `spec.bmc.address`
            — or `None` when no address is known.
    """
    ip = _opt_str(idrac_ip)
    if not ip:
        return None
    return f"idrac-virtualmedia://{ip}/redfish/v1/Systems/System.Embedded.1"


# OME NIC-port link status onto `LinkState`'s value set. OME has reported
# this as either a display string or a numeric code; both are handled. The
# numeric map is a best guess and must be confirmed on hardware — see
# docs/dell-collectors.md, "NIC status".
_LINK_STATUS_TEXT_MAP = {
    "connected": "UP",
    "up": "UP",
    "link up": "UP",
    "linkup": "UP",
    "disconnected": "DOWN",
    "down": "DOWN",
    "link down": "DOWN",
    "no link": "DOWN",
    "disabled": "DISABLED",
}
_LINK_STATUS_CODE_MAP = {1: "UP", 2: "DOWN"}


def _link_state(port: dict[str, Any]) -> str:
    """
    Map an OME NIC port's `LinkStatus` onto `LinkState`'s value set.

    Args:
        port (dict[str, Any]): One port under a `serverNetworkInterfaces`
            entry.

    Returns:
        str: "UP", "DOWN", "DISABLED", or "UNKNOWN". See
            docs/dell-collectors.md, "NIC status", for why the numeric
            encoding is treated as an unverified assumption.
    """
    raw = port.get("LinkStatus")
    if isinstance(raw, bool):
        return "UNKNOWN"
    if isinstance(raw, int):
        return _LINK_STATUS_CODE_MAP.get(raw, "UNKNOWN")
    return _LINK_STATUS_TEXT_MAP.get(str(raw or "").strip().lower(), "UNKNOWN")


def _port_speed_mbps(port: dict[str, Any]) -> int | None:
    """
    An OME NIC port's link speed in Mbps.

    Args:
        port (dict[str, Any]): One port under a `serverNetworkInterfaces`
            entry.

    Returns:
        int | None: The `LinkSpeed`, assumed already in Mbps, or `None` when
            absent or zero. See docs/dell-collectors.md, "NIC status".
    """
    speed = _as_int(port.get("LinkSpeed"))
    return speed or None


def nics_from_interfaces(interfaces: list[dict[str, Any]]) -> tuple[ProviderNic, ...]:
    """
    Every host NIC OME reports for a device, with link status and speed.

    OME nests `serverNetworkInterfaces` as interface -> `Ports` ->
    `Partitions`; the MAC is per partition (`CurrentMacAddress`) while link
    status and speed are per port and shared by that port's partitions. One
    `ProviderNic` is produced per partition, named by its `Fqdd` (or the
    `NicId`/`PortId` pair as a fallback).

    This deliberately differs from the production DHCP scanner, which
    selected a single PXE-boot MAC by server-name heuristics — that choice
    was specific to DHCP and has no field on `ProviderServer`. See
    docs/dell-collectors.md, "NIC status".

    Args:
        interfaces (list[dict[str, Any]]): The `serverNetworkInterfaces`
            `InventoryInfo` entries.

    Returns:
        tuple[ProviderNic, ...]: One NIC per partition, in traversal order.
    """
    nics: list[ProviderNic] = []
    for interface in interfaces:
        nic_id = _opt_str(interface.get("NicId")) or _opt_str(interface.get("Id"))
        ports = interface.get("Ports")
        for port in ports if isinstance(ports, list) else []:
            if not isinstance(port, dict):
                continue
            link_state = _link_state(port)
            speed_mbps = _port_speed_mbps(port)
            port_id = _opt_str(port.get("PortId"))
            partitions = port.get("Partitions")
            for partition in partitions if isinstance(partitions, list) else []:
                if not isinstance(partition, dict):
                    continue
                name = _opt_str(partition.get("Fqdd")) or "-".join(
                    part for part in (nic_id, port_id) if part
                )
                nics.append(
                    ProviderNic(
                        name=name or "nic",
                        mac=_opt_str(partition.get("CurrentMacAddress")),
                        speed_mbps=speed_mbps,
                        link_state=link_state,
                    )
                )
    return tuple(nics)


def nic_macs_from_nics(nics: tuple[ProviderNic, ...]) -> tuple[str, ...]:
    """
    The distinct MAC set for identity correlation, derived from `nics`.

    Args:
        nics (tuple[ProviderNic, ...]): The device's NICs.

    Returns:
        tuple[str, ...]: Each NIC's MAC once, in order, blanks skipped.
    """
    return tuple(dict.fromkeys(nic.mac for nic in nics if nic.mac))


# OME `serverProcessors` field names seen carrying a socket's logical
# processor (thread) count. They vary by OME/iDRAC version, so the first
# populated one wins. See docs/dell-collectors.md, "CPU summary".
_THREAD_COUNT_KEYS = (
    "NumberOfLogicalProcessors",
    "LogicalProcessorCount",
    "NumberOfThreads",
    "ThreadCount",
)
_HYPERTHREADING_KEYS = ("HyperThreadingEnabled", "HyperThreadingCapable")


def _logical_processors(processor: dict[str, Any]) -> int:
    """
    A socket's thread count from whichever field OME populated.

    Args:
        processor (dict[str, Any]): One `serverProcessors` entry.

    Returns:
        int: The first populated thread-count field, or `0` if none is
            present.
    """
    for key in _THREAD_COUNT_KEYS:
        value = _as_int(processor.get(key))
        if value:
            return value
    return 0


def _hyperthreading_on(processor: dict[str, Any]) -> bool:
    """
    Whether a socket reports hyperthreading, used to derive threads when OME
    gives no explicit logical-processor count.

    Args:
        processor (dict[str, Any]): One `serverProcessors` entry.

    Returns:
        bool: `True` if any known HT flag is truthy.
    """
    for key in _HYPERTHREADING_KEYS:
        value = processor.get(key)
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, str) and value.strip().lower() in ("true", "enabled", "yes", "1"):
            return True
    return False


def cpu_from_processors(processors: list[dict[str, Any]]) -> tuple[int, int, int, str | None]:
    """
    Summarize a device's CPUs from its `serverProcessors` inventory.

    Args:
        processors (list[dict[str, Any]]): The `serverProcessors`
            `InventoryInfo` entries, one per socket.

    Returns:
        tuple[int, int, int, str | None]: `(sockets, cores, threads, model)`.
            `sockets` is the entry count; `cores`/`threads` sum across
            sockets; `model` is the first entry's fullest available name.
            When OME reports no per-socket thread count but flags
            hyperthreading, threads falls back to `2 * cores` — see
            docs/dell-collectors.md, "CPU summary".
    """
    sockets = len(processors)
    cores = sum(_as_int(p.get("NumberOfCores")) for p in processors)
    threads = sum(_logical_processors(p) for p in processors)
    if threads == 0 and cores > 0 and any(_hyperthreading_on(p) for p in processors):
        threads = cores * 2
    model: str | None = None
    for p in processors:
        model = (
            _opt_str(p.get("ModelName"))
            or _opt_str(p.get("BrandName"))
            or _opt_str(p.get("Family"))
        )
        if model:
            break
    return sockets, cores, threads, model


def memory_bytes_from_modules(modules: list[dict[str, Any]]) -> int:
    """
    Total installed memory in bytes from `serverMemoryDevices` inventory.

    Args:
        modules (list[dict[str, Any]]): The `serverMemoryDevices`
            `InventoryInfo` entries.

    Returns:
        int: Sum of each module's `Size` (megabytes) converted to bytes.
            See docs/dell-collectors.md, "Capacity units", for why `Size`
            is read as MB.
    """
    return sum(_as_int(m.get("Size")) for m in modules) * _BYTES_PER_MB


_MEDIA_TYPE_MAP = {"hdd": "HDD", "ssd": "SSD", "nvme": "NVME"}

# OME rollup status codes and status strings mapped onto `HealthSeverity`.
# See docs/dell-collectors.md, "Drive health".
_STATUS_CODE_MAP = {1000: "HEALTHY", 2000: "UNKNOWN", 3000: "WARNING", 4000: "CRITICAL"}
_STATUS_TEXT_MAP = {
    "healthy": "HEALTHY",
    "ok": "HEALTHY",
    "normal": "HEALTHY",
    "warning": "WARNING",
    "critical": "CRITICAL",
    "error": "CRITICAL",
}


def _media_type(device: dict[str, Any]) -> str:
    """
    Map an OME disk's `MediaType` onto the platform's `MediaType`.

    Args:
        device (dict[str, Any]): One `serverStorage` `InventoryInfo` entry.

    Returns:
        str: HDD, SSD, NVME, or UNKNOWN.
    """
    return _MEDIA_TYPE_MAP.get(str(device.get("MediaType", "") or "").strip().lower(), "UNKNOWN")


def _drive_health(device: dict[str, Any]) -> str:
    """
    Map an OME disk's rollup status onto the platform's `HealthSeverity`.

    Args:
        device (dict[str, Any]): One `serverStorage` `InventoryInfo` entry.
            OME reports status either as a numeric rollup code (`Status`) or
            a string (`PrimaryStatus`/`Status`); both are handled.

    Returns:
        str: HEALTHY, WARNING, CRITICAL, or UNKNOWN.
    """
    raw = device.get("Status")
    if isinstance(raw, int):
        return _STATUS_CODE_MAP.get(raw, "UNKNOWN")
    text = str(device.get("PrimaryStatus") or device.get("Status") or "").strip().lower()
    return _STATUS_TEXT_MAP.get(text, "UNKNOWN")


def _disk_capacity_bytes(value: object) -> int | None:
    """
    Parse a `serverArrayDisks` `Size` into bytes.

    OME reports this either as a plain byte count or as a "<number> <unit>"
    string (e.g. `"960.0 GB"`); both are handled. See
    docs/dell-collectors.md, "Capacity units".

    Args:
        value (object): The raw `Size` field.

    Returns:
        int | None: Capacity in bytes, or `None` when absent or unparseable.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) or None
    match = _SIZE_RE.match(str(value).strip())
    if not match:
        return None
    number, unit = match.group(1), (match.group(2) or "B").upper()
    try:
        return int(float(number) * _UNIT_BYTES.get(unit, 1)) or None
    except ValueError:
        return None


def storage_from_devices(
    devices: list[dict[str, Any]],
) -> tuple[tuple[dict[str, object], ...], int]:
    """
    Summarize a device's physical disks from its `serverArrayDisks`
    inventory.

    Args:
        devices (list[dict[str, Any]]): The `serverArrayDisks`
            `InventoryInfo` entries.

    Returns:
        tuple[tuple[dict[str, object], ...], int]: `(drives, total_bytes)`.
            A disk whose `Size` cannot be read still contributes a drive
            entry with `capacity_bytes=None` and adds nothing to the total.
    """
    drives: list[dict[str, object]] = []
    total_bytes = 0
    for device in devices:
        capacity_bytes = _disk_capacity_bytes(device.get("Size"))
        total_bytes += capacity_bytes or 0
        drives.append(
            {
                "id": str(
                    device.get("Id")
                    or device.get("DiskNumber")
                    or device.get("Slot")
                    or device.get("Name")
                    or ""
                ),
                "model": _opt_str(device.get("ModelNumber") or device.get("Model")),
                "serial": _opt_str(device.get("SerialNumber")),
                "media_type": _media_type(device),
                "capacity_bytes": capacity_bytes,
                "health": _drive_health(device),
            }
        )
    return tuple(drives), total_bytes


def to_provider_server(
    *,
    profile: dict[str, Any],
    device: dict[str, Any],
    processors: list[dict[str, Any]],
    memory_modules: list[dict[str, Any]],
    storage: list[dict[str, Any]],
    network_interfaces: list[dict[str, Any]],
    manager_id: str,
) -> ProviderServer:
    """
    Convert one OME profile and its joined device inventory into a
    `ProviderServer`.

    Args:
        profile (dict[str, Any]): One `/ProfileService/Profiles` entry;
            `ProfileName` is the server name, `TargetName` its iDRAC IP, and
            `TemplateName`/`TemplateId` the deployment template it came from.
        device (dict[str, Any]): The `/DeviceService/Devices` entry joined
            by iDRAC IP, or `{}` when OME has no managed device for the
            profile. `Model` and `DeviceServiceTag` come from here.
        processors (list[dict[str, Any]]): `serverProcessors` inventory.
        memory_modules (list[dict[str, Any]]): `serverMemoryDevices`
            inventory.
        storage (list[dict[str, Any]]): `serverStorage` inventory.
        network_interfaces (list[dict[str, Any]]):
            `serverNetworkInterfaces` inventory.
        manager_id (str): The manager this server is reported under.

    Returns:
        ProviderServer: The vendor-neutral DTO the ingest pipeline consumes.
            The site is intentionally not set here — it is parsed from the
            name downstream.
    """
    name = _opt_str(profile.get("ProfileName")) or ""
    idrac_ip = _opt_str(profile.get("TargetName")) or _opt_str(device.get("DeviceName"))
    serial = _opt_str(device.get("DeviceServiceTag"))

    sockets, cores, threads, cpu_model = cpu_from_processors(processors)
    storage_drives, storage_total_bytes = storage_from_devices(storage)
    nics = nics_from_interfaces(network_interfaces)

    return ProviderServer(
        # The service tag identifies the physical machine; the profile name
        # is the stable fallback when OME has no managed device for it (so
        # no service tag), which also keeps `external_id` present for the
        # correlation ladder's per-manager step.
        external_id=serial or f"ome-profile:{name}",
        vendor=_VENDOR,
        name=name,
        model=_opt_str(device.get("Model")),
        serial=serial,
        nic_macs=nic_macs_from_nics(nics),
        nics=nics,
        bmc_address_raw=idrac_bmc_address(idrac_ip),
        manager_id=manager_id,
        # The OME deployment template this profile was created from, when
        # any. See docs/dell-collectors.md, "Profile template".
        profile_template_name=_opt_str(profile.get("TemplateName")),
        profile_template_external_id=_opt_str(profile.get("TemplateId")),
        cpu_sockets=sockets,
        cpu_cores=cores,
        cpu_threads=threads,
        cpu_model=cpu_model,
        memory_total_bytes=memory_bytes_from_modules(memory_modules),
        storage_total_bytes=storage_total_bytes,
        storage_drives=storage_drives,
    )
