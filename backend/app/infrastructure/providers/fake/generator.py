"""Deterministic fake inventory data.

Produces `ProviderServer` DTOs (the same shape a real Dell OpenManage /
Cisco UCS Manager / HPE OneView collector will produce) across Dell/Cisco/
HPE vendors, with realistic-shaped names, varying hardware, and — for
Cisco — fabric-interconnect connectivity attachments.

Two independent axes of "realistic" are deliberately kept separate:

* Server *names* follow patterns (`ocp-<vendor>-<role>-NNN`,
  `upi-<vendor>-<role>-NNN`, `random-server-NNNN`) that *look like* what a
  real installation-type classification engine would key on — but
  classification itself is a slice-2 concern. Nothing here sets
  `Classification.installation_type`; every generated server ends up
  `UNCLASSIFIED` until slice 2's engine runs against it. The names exist
  so slice 2 has realistic fixtures to classify, not because this module
  classifies anything.
* Hardware/connectivity shape (drive counts, health, fabric attachment
  counts) varies per server so the health engine (slice 3) and the API's
  list/filter/sort paths have non-uniform data to exercise.

Determinism: every random choice goes through a single `random.Random(seed)`
instance threaded through in a fixed order, so `generate_servers(seed=42,
count=1000)` produces byte-identical `ProviderServer` output on every run,
on every machine — this is what makes `tools/seed_inventory.py --seed 42`
reproducible and is asserted directly in
`tests/unit/infrastructure/providers/test_generator.py`.

GPU note: `ProviderServer` (see `app.domain.ports.provider`) has no GPU
field, only `cpu_*`/`memory_total_bytes`/`storage_*`/`attachments`. The
plan for this slice called for "varying hardware (some GPUs, some not)",
but the provider DTO contract — which this module must produce exactly,
unmodified — has no seam for it. Rather than bolt GPU data onto an
unrelated field (e.g. smuggling it into `tags`), this generator omits GPUs
entirely and flags the gap in the slice's final report; every generated
`Hardware.gpus` ends up `[]`. Adding a `gpu_*`/`gpus` field to
`ProviderServer` is a domain-port change out of this slice's scope.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Iterator

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.models.site import Site
from app.domain.ports.provider import ProviderAttachment, ProviderServer
from app.utils.ids import ID_PREFIXES

# --- Fixed reference universe -----------------------------------------
#
# Sites/managers are a small, fixed set independent of `seed`/`count` —
# re-seeding with a different seed or count must not create duplicate
# site/manager documents (both have a unique index on `name`). Only the
# *servers* generated vary with `seed`/`count`.

_SITE_CODES = ("ams1", "fra2", "nyc3", "sjc4")
_SITE_NAMES = {
    "ams1": "Amsterdam DC1",
    "fra2": "Frankfurt DC2",
    "nyc3": "New York DC3",
    "sjc4": "San Jose DC4",
}


def _site_id(code: str) -> str:
    return f"{ID_PREFIXES['site']}_fake_{code}"


def _manager_id(slug: str) -> str:
    return f"{ID_PREFIXES['manager']}_fake_{slug}"


def list_sites() -> list[Site]:
    """The fixed set of fake sites servers are distributed across."""
    return [
        Site(
            id=_site_id(code),
            name=_SITE_NAMES[code],
            code=code.upper(),
            audit=AuditFields.new(),
        )
        for code in _SITE_CODES
    ]


def list_managers() -> list[Manager]:
    """The fixed set of fake managers, including one UCS Central ->
    UCS Manager parent/child pair per the plan's requirement that at
    least one such hierarchy exist in the seed data.
    """
    central_id = _manager_id("ucs-central-global")
    managers = [
        Manager(
            id=central_id,
            name="ucs-central-global",
            type=ManagerType.UCS_CENTRAL,
            site_id=None,
            parent_manager_id=None,
            audit=AuditFields.new(),
        )
    ]
    for code in _SITE_CODES:
        site_id = _site_id(code)
        managers.append(
            Manager(
                id=_manager_id(f"ucsm-{code}"),
                name=f"ucsm-{code}",
                type=ManagerType.UCS_MANAGER,
                site_id=site_id,
                parent_manager_id=central_id,
                audit=AuditFields.new(),
            )
        )
        managers.append(
            Manager(
                id=_manager_id(f"ome-{code}"),
                name=f"ome-{code}",
                type=ManagerType.OPENMANAGE,
                site_id=site_id,
                parent_manager_id=None,
                audit=AuditFields.new(),
            )
        )
        managers.append(
            Manager(
                id=_manager_id(f"oneview-{code}"),
                name=f"oneview-{code}",
                type=ManagerType.ONEVIEW,
                site_id=site_id,
                parent_manager_id=None,
                audit=AuditFields.new(),
            )
        )
    return managers


def _manager_for(vendor: str, code: str) -> str:
    if vendor == "dell":
        return _manager_id(f"ome-{code}")
    if vendor == "cisco":
        return _manager_id(f"ucsm-{code}")
    return _manager_id(f"oneview-{code}")  # hpe


# --- Vendor-specific catalogs -------------------------------------------

_VENDORS = ("dell", "cisco", "hpe")

_MODELS: dict[str, tuple[str, ...]] = {
    "dell": ("PowerEdge R650", "PowerEdge R750", "PowerEdge R6515"),
    "cisco": ("UCS C220 M6", "UCS C240 M6", "UCS B200 M6"),
    "hpe": ("ProLiant DL380 Gen11", "ProLiant DL360 Gen11", "ProLiant DL325 Gen11"),
}

_CPU_MODELS: dict[str, tuple[str, ...]] = {
    "dell": ("Intel Xeon Gold 6338", "Intel Xeon Platinum 8358"),
    "cisco": ("Intel Xeon Gold 6348", "Intel Xeon Silver 4314"),
    "hpe": ("AMD EPYC 7513", "AMD EPYC 9354"),
}

_NAME_ROLES = ("master", "worker")

# (pattern-family, weight) — weighted so "unclassified-shaped" names are a
# meaningful minority, matching a realistic mixed estate rather than an
# evenly-split one.
_NAME_FAMILIES = ("hosted_cluster", "hosted_cluster", "upi", "upi", "unclassified")

_DRIVE_MEDIA = ("NVME", "SSD", "SSD", "HDD")
_DRIVE_HEALTH = ("OK", "OK", "OK", "OK", "DEGRADED", "FAILED")
_DRIVE_CAPACITIES_BYTES: dict[str, tuple[int, ...]] = {
    "NVME": (960_000_000_000, 1_920_000_000_000, 3_840_000_000_000),
    "SSD": (480_000_000_000, 960_000_000_000, 1_920_000_000_000),
    "HDD": (2_000_000_000_000, 4_000_000_000_000, 8_000_000_000_000),
}

_ATTACHMENT_COUNTS = (0, 1, 2, 2, 2, 4)  # weighted toward the common 2-up case
_OPER_STATE_PATTERNS: dict[int, tuple[tuple[str, ...], ...]] = {
    1: (("UP",), ("DOWN",)),
    2: (("UP", "UP"), ("UP", "DOWN"), ("DOWN", "DOWN")),
    4: (("UP", "UP", "UP", "UP"), ("UP", "UP", "UP", "DOWN"), ("UP", "DOWN", "UP", "DOWN")),
}


def _random_mac(rng: random.Random) -> str:
    value = rng.getrandbits(48)
    if value in (0, (1 << 48) - 1):  # avoid the reserved all-zero/broadcast forms
        value ^= 1
    hexed = f"{value:012x}"
    return ":".join(hexed[i : i + 2] for i in range(0, 12, 2))


def _random_uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _bmc_address(vendor: str, ip: str) -> str:
    if vendor == "dell":
        return f"idrac-virtualmedia://{ip}/redfish/v1/Systems/System.Embedded.1"
    if vendor == "hpe":
        return f"redfish-virtualmedia://{ip}/redfish/v1/Systems/1"
    return f"ipmi://{ip}:623"  # cisco


def _fake_ip(rng: random.Random, site_index: int, host_index: int) -> str:
    return f"10.{site_index + 10}.{host_index // 256}.{host_index % 256}"


# Per-vendor template-name pools, in each platform's own naming style —
# UCS Manager's Service Profile Templates, Intersight's Server Profile
# Templates, HPE OneView's Server Profile Templates, and Dell OME's
# Deployment Templates (see `app.domain.models.server.ProfileTemplate`'s
# docstring for the full per-vendor mapping this DTO field feeds into).
_TEMPLATE_NAMES: dict[str, tuple[str, ...]] = {
    "cisco": ("SPT-OCP-Worker-B200", "SPT-OCP-Master-C240", "SPT-UPI-Generic"),
    "dell": ("DT-OCP-Worker-R760", "DT-OCP-Master-R760", "DT-UPI-Baseline"),
    "hpe": ("SPT-OCP-Worker-DL380", "SPT-OCP-Master-DL380", "SPT-UPI-Baseline"),
}


def _profile_template(rng: random.Random, vendor: str) -> tuple[str | None, str | None]:
    """Returns `(name, external_id)`. ~10% of fake servers get no
    template at all (a server profile applied ad hoc, not from a
    template — a real, common state on all four platforms), matching how
    every other optional field in this generator models "sometimes
    absent" rather than always-populated.
    """
    if rng.random() < 0.1:
        return None, None
    name = rng.choice(_TEMPLATE_NAMES[vendor])
    if vendor == "cisco":
        external_id = name  # UCS Manager references templates by name (srcTemplName)
    elif vendor == "hpe":
        external_id = f"/rest/server-profile-templates/{rng.getrandbits(32):08x}"
    else:
        external_id = str(rng.randint(1000, 9999))  # OME TemplateId
    return name, external_id


def _build_name(rng: random.Random, vendor: str, index: int) -> str:
    family = rng.choice(_NAME_FAMILIES)
    if family == "hosted_cluster":
        role = rng.choice(_NAME_ROLES)
        return f"ocp-{vendor}-{role}-{index:03d}"
    if family == "upi":
        role = rng.choice(_NAME_ROLES)
        return f"upi-{vendor}-{role}-{index:03d}"
    return f"random-server-{index:04d}"


def _build_storage_drives(
    rng: random.Random, count: int
) -> tuple[tuple[dict[str, object], ...], int]:
    drives: list[dict[str, object]] = []
    total = 0
    for i in range(count):
        media = rng.choice(_DRIVE_MEDIA)
        capacity = rng.choice(_DRIVE_CAPACITIES_BYTES[media])
        health = rng.choice(_DRIVE_HEALTH)
        total += capacity
        drives.append(
            {
                "id": f"disk{i}",
                "model": f"{media}-{capacity // 1_000_000_000}G",
                "serial": f"drv{rng.getrandbits(32):08x}",
                "media_type": media,
                "capacity_bytes": capacity,
                "health": health,
            }
        )
    return tuple(drives), total


def _build_attachments(rng: random.Random, *, site_code: str) -> tuple[ProviderAttachment, ...]:
    count = rng.choice(_ATTACHMENT_COUNTS)
    if count == 0:
        return ()

    patterns = _OPER_STATE_PATTERNS[count]
    oper_states = rng.choice(patterns)

    attachments: list[ProviderAttachment] = []
    for i, oper_state in enumerate(oper_states):
        fabric = "A" if i % 2 == 0 else "B"
        fabric_name = f"FI-{fabric}-{site_code}-01"
        attachments.append(
            ProviderAttachment(
                type="FABRIC_INTERCONNECT",
                provider="UCS_MANAGER",
                fabric=fabric,
                fabric_name=fabric_name,
                fabric_id=fabric_name,
                fabric_model="UCS-FI-6454",
                fabric_serial=f"FI{rng.getrandbits(24):06x}",
                server_interface=f"eth{i}",
                server_port=f"1/{i + 1}",
                fabric_port=f"1/{i + 1}",
                admin_state="ENABLED",
                oper_state=oper_state,
                speed_mbps=rng.choice((10_000, 25_000, 40_000)),
            )
        )
    return tuple(attachments)


def generate_servers(*, seed: int, count: int) -> Iterator[ProviderServer]:
    """Yield `count` deterministic `ProviderServer` DTOs for the given
    `seed`. Every random decision is drawn from one `random.Random(seed)`
    in a fixed order, so this is fully reproducible: same `(seed, count)`
    -> byte-identical (field-for-field equal) output, every run.
    """
    rng = random.Random(seed)  # noqa: S311 - deterministic fake data, never security-sensitive
    sites = _SITE_CODES

    for index in range(count):
        site_index = index % len(sites)
        site_code = sites[site_index]
        vendor = _VENDORS[index % len(_VENDORS)]
        # Shuffle vendor pick slightly so it isn't perfectly periodic —
        # still fully deterministic (drawn from `rng`), just less uniform.
        if rng.random() < 0.15:
            vendor = rng.choice(_VENDORS)

        name = _build_name(rng, vendor, index)
        model = rng.choice(_MODELS[vendor])
        serial = f"{vendor[:3].upper()}{index:07d}"
        system_uuid = _random_uuid(rng)

        nic_count = rng.randint(2, 4)
        nic_macs = tuple(_random_mac(rng) for _ in range(nic_count))
        bmc_mac = _random_mac(rng)
        bmc_ip = _fake_ip(rng, site_index, index)
        bmc_address_raw = _bmc_address(vendor, bmc_ip)

        cpu_sockets = rng.choice((1, 2))
        cpu_cores = rng.choice((16, 24, 32, 48, 64))
        cpu_threads = cpu_cores * 2
        cpu_model = rng.choice(_CPU_MODELS[vendor])

        memory_gib = rng.choice((128, 256, 512, 1024))
        memory_total_bytes = memory_gib * 1024**3

        drive_count = rng.randint(2, 8)
        storage_drives, storage_total_bytes = _build_storage_drives(rng, drive_count)

        attachments = _build_attachments(rng, site_code=site_code) if vendor == "cisco" else ()
        template_name, template_external_id = _profile_template(rng, vendor)

        tags: tuple[str, ...] = ()
        if rng.random() < 0.3:
            tags = (f"rack-{rng.randint(1, 40)}",)

        yield ProviderServer(
            external_id=f"fake-{seed}-{index:07d}",
            vendor=vendor,
            name=name,
            model=model,
            serial=serial,
            system_uuid=system_uuid,
            nic_macs=nic_macs,
            bmc_address_raw=bmc_address_raw,
            bmc_mac=bmc_mac,
            site_id=_site_id(site_code),
            manager_id=_manager_for(vendor, site_code),
            profile_template_name=template_name,
            profile_template_external_id=template_external_id,
            cpu_sockets=cpu_sockets,
            cpu_cores=cpu_cores,
            cpu_threads=cpu_threads,
            cpu_model=cpu_model,
            memory_total_bytes=memory_total_bytes,
            storage_total_bytes=storage_total_bytes,
            storage_drives=storage_drives,
            attachments=attachments,
            tags=tags,
        )
