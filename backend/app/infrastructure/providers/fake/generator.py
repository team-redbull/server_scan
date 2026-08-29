"""Deterministic fake inventory data.

Produces `ProviderServer` DTOs in the shapes the three collectors that
actually exist emit, so dev and CI fixtures exercise the same field
shapes, vocabularies and absences production does:

* `UCS_CENTRAL` — Cisco blades, which live in a UCS domain registered
  with Central.
* `INTERSIGHT` — Cisco rack units, the ones claimed directly into
  Intersight rather than into a UCS domain. This mirrors the real
  partition: the Intersight collector excludes `ManagementMode == UCSM`
  precisely because UCS Central already owns those servers
  (docs/adr/0017-intersight-collector.md, "Decision 3").
* `REDFISH_STANDALONE` — everything else, one BMC at a time.

A field a collector cannot read is `None` here too, and each collector's
absences differ, which is the point: UCS Manager reports no GPUs at all,
while Intersight reports a GPU's identity with none of its telemetry, and
only Redfish reports both. A fixture richer than the real thing hides
exactly the gaps worth seeing.

Determinism: every random choice goes through a single `random.Random(seed)`
instance threaded through in a fixed order, so `generate_servers(seed=42,
count=1000)` produces byte-identical `ProviderServer` output on every run,
on every machine — this is what makes `tools/seed_inventory.py --seed 42`
reproducible and is asserted directly in
`tests/unit/infrastructure/providers/test_generator.py`.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Iterator
from dataclasses import replace

from app.domain.enums import ManagerType, SiteCode
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.models.site import Site
from app.domain.ports.provider import ProviderAttachment, ProviderServer
from app.domain.value_objects.site import SITE_DISPLAY_NAMES

# --- Fixed reference universe -----------------------------------------
#
# Sites/managers are a small, fixed set independent of `seed`/`count` —
# re-seeding with a different seed or count must not create duplicate
# site/manager documents (both have a unique index on `name`). Only the
# *servers* generated vary with `seed`/`count`.

_SITE_CODES = tuple(member.value for member in SiteCode)

# The manager types that have a collector. `OPENMANAGE` and `ONEVIEW`
# have configuration slots and no implementation, so seeding servers
# behind them would invent a data path that cannot exist.
COLLECTOR_TYPES: tuple[ManagerType, ...] = (
    ManagerType.UCS_CENTRAL,
    ManagerType.INTERSIGHT,
    ManagerType.REDFISH_STANDALONE,
)


def _site_id(code: str) -> str:
    """A site's id *is* its code.

    No `site_fake_<code>` prefix: a server's site is derived from its name
    (`app.domain.value_objects.site.parse_site_code`), which can only ever
    yield a bare `SiteCode`. Prefixing the `Site` document's id would mean
    the id a server carries and the id a site document has are different
    strings — which is exactly the mismatch that made filtering by site
    silently return nothing.

    Args:
        code (str): A `SiteCode` value.

    Returns:
        str: The `Site` document id.
    """
    return code


def manager_id_for(manager_type: ManagerType) -> str:
    """
    The `Manager` document id a collector of this type writes.

    Deliberately identical to `tools.run_collector.manager_for`'s id, so a
    seeded fleet and a really-collected one resolve `Server.manager_id`
    through the same document instead of two near-duplicates.

    Args:
        manager_type (ManagerType): The collector's manager type.

    Returns:
        str: The deterministic manager id.
    """
    return f"mgr_{manager_type.value.lower()}"


def _is_blade(model: str) -> bool:
    """
    Whether a Cisco model is a B-series blade rather than a rack unit.

    Args:
        model (str): The model string, e.g. `"UCS B200 M6"`.

    Returns:
        bool: True for a blade.
    """
    parts = model.split()
    return len(parts) > 1 and parts[1].startswith("B")


def collector_for(vendor: str, model: str) -> ManagerType:
    """
    Which collector would really have found this server.

    Cisco is split the way the two Cisco collectors actually split it:
    a blade sits in a chassis inside a UCS domain, which is what UCS
    Central registers and collects; a rack unit is the shape claimed
    directly into Intersight. Nothing is collected by both, mirroring the
    `ManagementMode` partition the Intersight collector enforces.

    Args:
        vendor (str): A `Vendor` value.
        model (str): The server's model.

    Returns:
        ManagerType: The owning collector.
    """
    if vendor != "cisco":
        return ManagerType.REDFISH_STANDALONE
    if _is_blade(model):
        return ManagerType.UCS_CENTRAL
    return ManagerType.INTERSIGHT


def provider_type_for(server: ProviderServer) -> str:
    """
    Which collector owns an already-generated server.

    Read back off `external_id` rather than recomputed from the server's
    fields, because `external_id` is the one thing a real collector
    stamps with its own identity — so this cannot drift from what
    `collector_for` decided when the server was built.

    Args:
        server (ProviderServer): A generated server.

    Returns:
        str: The owning collector's `ManagerType` value.
    """
    if server.external_id.startswith("intersight/"):
        return ManagerType.INTERSIGHT.value
    if server.external_id.startswith("compute/sys-"):
        return ManagerType.UCS_CENTRAL.value
    return ManagerType.REDFISH_STANDALONE.value


def list_sites() -> list[Site]:
    """
    The fixed set of sites servers are distributed across.

    Returns:
        list[Site]: One `Site` per `SiteCode`, named as the API names it.
    """
    return [
        Site(
            id=_site_id(code),
            name=SITE_DISPLAY_NAMES[SiteCode(code)],
            code=code.upper(),
            audit=AuditFields.new(),
        )
        for code in _SITE_CODES
    ]


def list_managers() -> list[Manager]:
    """
    The managers the seeded fleet is collected through.

    One document per implemented collector, mirroring the projection
    `tools.run_collector.manager_for` writes on every real run. There is
    no UCS Central -> UCS Manager pair here any more: `--manager-type
    UCS_MANAGER` was removed, and Central is the single Cisco entry point.

    Returns:
        list[Manager]: The `UCS_CENTRAL` and `REDFISH_STANDALONE` managers.
    """
    return [
        Manager(
            id=manager_id_for(manager_type),
            name=manager_type.value.lower().replace("_", "-"),
            type=manager_type,
            endpoint=None,
            enabled=True,
            audit=AuditFields.new(),
        )
        for manager_type in COLLECTOR_TYPES
    ]


# --- Vendor-specific catalogs -------------------------------------------

# `standalone` is a real vendor, not a fallback: a Lenovo/Supermicro/
# whitebox machine reached at its own BMC, which is what the Redfish
# collector maps any manufacturer this platform does not model onto.
_VENDORS = ("dell", "cisco", "hp", "standalone")

_MODELS: dict[str, tuple[str, ...]] = {
    "dell": ("PowerEdge R650", "PowerEdge R750", "PowerEdge R6515"),
    "cisco": ("UCS C220 M6", "UCS C240 M6", "UCS B200 M6"),
    "hp": ("ProLiant DL380 Gen11", "ProLiant DL360 Gen11", "ProLiant DL325 Gen11"),
    "standalone": ("ThinkSystem SR650 V3", "SYS-221H-TNR", "AS-2125HS-TNR"),
}

_CPU_MODELS: dict[str, tuple[str, ...]] = {
    "dell": ("Intel Xeon Gold 6338", "Intel Xeon Platinum 8358"),
    "cisco": ("Intel Xeon Gold 6348", "Intel Xeon Silver 4314"),
    "hp": ("AMD EPYC 7513", "AMD EPYC 9354"),
    "standalone": ("Intel Xeon Gold 6438Y+", "AMD EPYC 9454"),
}

# UPI node roles, in the real cluster vocabulary.
_NAME_ROLES = ("compute", "control-plane", "infra")

# Environment segment some UPI hostnames carry (`ocp4-prod-tlv-infra-01`),
# and some don't (`ocp4-nyc-control-plane-02`). Both real shapes.
_NAME_ENVIRONMENTS = ("prod", "prep", None)

# (pattern-family, weight) — weighted so "unclassified-shaped" names stay
# a small minority, matching a realistic mixed estate. The unclassified
# family deliberately carries no site token either, so the UI's
# "Unclassified" and "Unassigned site" states both get real fixtures
# instead of being unreachable in dev.
_NAME_FAMILIES = (
    "hosted_cluster",
    "hosted_cluster",
    "hosted_cluster_hw",
    "upi",
    "upi",
    "upi",
    "unclassified",
)

_DRIVE_MEDIA = ("NVME", "SSD", "SSD", "HDD")

# Both collectors normalize component health onto `HealthSeverity` at the
# provider boundary (`redfish.mapping.health_of`, `ucs_manager.mapping.
# _disk_health`), so fixtures must speak that vocabulary and not an
# invented OK/DEGRADED/FAILED one — the seeded storage policy counts
# CRITICAL drives.
_COMPONENT_HEALTHS = ("HEALTHY", "HEALTHY", "HEALTHY", "HEALTHY", "WARNING", "CRITICAL")

_DRIVE_CAPACITIES_BYTES: dict[str, tuple[int, ...]] = {
    "NVME": (960_000_000_000, 1_920_000_000_000, 3_840_000_000_000),
    "SSD": (480_000_000_000, 960_000_000_000, 1_920_000_000_000),
    "HDD": (2_000_000_000_000, 4_000_000_000_000, 8_000_000_000_000),
}

# (vendor, model, memory type, memory bytes) — the accelerators a Redfish
# `Processor` with `ProcessorType == "GPU"` actually reports.
_GPU_MODELS: tuple[tuple[str, str, str, int], ...] = (
    ("NVIDIA", "H100 80GB HBM3", "HBM3", 80 * 1024**3),
    ("NVIDIA", "H200 141GB HBM3e", "HBM3e", 141 * 1024**3),
    ("NVIDIA", "L40S", "GDDR6", 48 * 1024**3),
    ("AMD", "Instinct MI300X", "HBM3", 192 * 1024**3),
)
_GPU_COUNTS = (0, 0, 0, 0, 0, 0, 2, 4, 8)  # most servers have none

_ATTACHMENT_COUNTS = (0, 1, 2, 2, 2, 4)  # weighted toward the common 2-up case
_OPER_STATE_PATTERNS: dict[int, tuple[tuple[str, ...], ...]] = {
    1: (("UP",), ("DOWN",)),
    2: (("UP", "UP"), ("UP", "DOWN"), ("DOWN", "DOWN")),
    4: (("UP", "UP", "UP", "UP"), ("UP", "UP", "UP", "DOWN"), ("UP", "DOWN", "UP", "DOWN")),
}

# vNICs UCS carves out of each physical port (adaptorHostEthIf per
# adaptorExtEthIf). Two is the common OCP bond.
_VNICS_PER_PORT = 2


def _random_mac(rng: random.Random) -> str:
    """
    A syntactically valid random MAC.

    Args:
        rng (random.Random): The seeded generator.

    Returns:
        str: A colon-separated MAC.
    """
    value = rng.getrandbits(48)
    if value in (0, (1 << 48) - 1):  # avoid the reserved all-zero/broadcast forms
        value ^= 1
    hexed = f"{value:012x}"
    return ":".join(hexed[i : i + 2] for i in range(0, 12, 2))


def _random_uuid(rng: random.Random) -> str:
    """
    A random system UUID.

    Args:
        rng (random.Random): The seeded generator.

    Returns:
        str: The UUID in canonical string form.
    """
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _bmc_address(collector: ManagerType, ip: str) -> str:
    """
    The BMC address the owning collector would report.

    Args:
        collector (ManagerType): The collector that owns the server.
        ip (str): Its BMC address.

    Returns:
        str: `ipmi://<ip>:623` for either Cisco collector — they read the
            same CIMC, and `intersight.mapping.bmc_address` deliberately
            emits the same form `ucs_manager.mapping` does — and the
            `redfish://` form `redfish.mapping` composes otherwise.
    """
    if collector is ManagerType.REDFISH_STANDALONE:
        return f"redfish://{ip}/redfish/v1/Systems/1"
    return f"ipmi://{ip}:623"


def _external_id(collector: ManagerType, *, index: int, site_index: int, bmc_ip: str) -> str:
    """
    The collector-native identifier for this server.

    Args:
        collector (ManagerType): The collector that owns it.
        index (int): Its index in the generated fleet.
        site_index (int): Its site's position in `_SITE_CODES`, standing in
            for a registered UCS domain.
        bmc_ip (str): Its BMC address.

    Returns:
        str: A domain-qualified UCS DN (`ucs_central.provider.
            central_external_id`'s shape), an `intersight/<moid>` id
            (`intersight.mapping.external_id`'s shape, whose Moid is 24
            hex characters), or the `redfish://` system URL.
    """
    if collector is ManagerType.UCS_CENTRAL:
        domain_id = str(1000 + site_index)
        return f"compute/sys-{domain_id}/chassis-{index // 8 + 1}/blade-{index % 8 + 1}"
    if collector is ManagerType.INTERSIGHT:
        return f"intersight/{index:024x}"
    return f"redfish://{bmc_ip}/redfish/v1/Systems/1"


def _fake_ip(site_index: int, host_index: int) -> str:
    """
    A stable BMC IP for one server.

    Args:
        site_index (int): Its site's position in `_SITE_CODES`.
        host_index (int): Its index in the generated fleet.

    Returns:
        str: A dotted-quad address in a per-site /16.
    """
    return f"10.{site_index + 10}.{host_index // 256}.{host_index % 256}"


# UCS Manager Service Profile Templates. Only Cisco gets one: the Redfish
# collector reads a BMC, which knows nothing about profiles, and the OME/
# OneView collectors that would report their own templates do not exist.
_TEMPLATE_NAMES: tuple[str, ...] = (
    "SPT-OCP-Worker-B200",
    "SPT-OCP-Master-C240",
    "SPT-UPI-Generic",
)


def _profile_template(rng: random.Random, vendor: str) -> tuple[str | None, str | None]:
    """
    The service profile template this server was deployed from.

    ~10% of Cisco servers get none — a service profile applied ad hoc
    rather than from a template is a real, common state.

    Args:
        rng (random.Random): The seeded generator.
        vendor (str): The server's vendor.

    Returns:
        tuple[str | None, str | None]: `(name, external_id)`; UCS Manager
            references a template by name, so both are the same string.
    """
    if vendor != "cisco" or rng.random() < 0.1:
        return None, None
    name = rng.choice(_TEMPLATE_NAMES)
    return name, name


def _profile_dn(collector: ManagerType, *, name: str, site_code: str) -> str | None:
    """
    The service profile's own DN, which doubles as its org path.

    The org segment is what `app.domain.value_objects.site.parse_site_code`
    falls back to when a server's *name* carries no site token, so the
    siteless name family still resolves to a site on UCS — exactly as it
    does in production.

    Only `UCS_CENTRAL` reports one. An Intersight `server.Profile` has no
    `Dn` field at all, so its servers get `None` and a siteless name
    there really does resolve to no site — a real gap this fixture is
    meant to show rather than paper over.

    Args:
        collector (ManagerType): The collector that owns the server.
        name (str): The server's name, which is its profile's name.
        site_code (str): The site whose org holds the profile.

    Returns:
        str | None: The DN, or `None` for a collector that reports none.
    """
    if collector is not ManagerType.UCS_CENTRAL:
        return None
    return f"org-root/org-{site_code}/ls-{name}"


def _build_name(
    rng: random.Random, vendor: str, index: int, site_code: str, model: str, memory_gib: int
) -> str:
    """
    A hostname in the shapes this estate actually uses.

    Every shape but the deliberate `unclassified` minority embeds
    `site_code` as a whole `-`-delimited token, because the name is what
    `app.domain.value_objects.site.parse_site_code` reads the site back
    out of, and what the seeded classification rules key on to decide
    HOSTED_CLUSTER vs UPI.

    Args:
        rng (random.Random): The seeded generator.
        vendor (str): The server's vendor.
        index (int): Its index in the generated fleet.
        site_code (str): The site token to embed.
        model (str): Its model, one hostname shape embeds a short form.
        memory_gib (int): Its memory, likewise embedded in that shape.

    Returns:
        str: The hostname.
    """
    family = rng.choice(_NAME_FAMILIES)

    if family == "hosted_cluster":
        # ocp4-hypershift-tlv-01 / ocp4-hypershift-data-tlv-02
        segment = "hypershift-data" if rng.random() < 0.35 else "hypershift"
        return f"ocp4-{segment}-{site_code}-{index % 100:02d}"

    if family == "hosted_cluster_hw":
        # ocp-dell-r650-tlv-128c-1024gb-<serial>
        short_model = model.split()[-1].lower()
        cores = rng.choice((64, 128, 192))
        return (
            f"ocp-{vendor}-{short_model}-{site_code}-{cores}c-"
            f"{memory_gib}gb-{vendor[:3].upper()}{index:07d}"
        )

    if family == "upi":
        # ocp4-five-compute-01 / ocp4-nyc-control-plane-02 /
        # ocp4-prod-tlv-infra-01
        role = rng.choice(_NAME_ROLES)
        environment = rng.choice(_NAME_ENVIRONMENTS)
        prefix = f"ocp4-{environment}" if environment else "ocp4"
        return f"{prefix}-{site_code}-{role}-{index % 100:02d}"

    # No site token and no classifiable shape, on purpose.
    return f"random-server-{index:04d}"


def _build_storage_drives(
    rng: random.Random, count: int
) -> tuple[tuple[dict[str, object], ...], int]:
    """
    A server's drives, keyed exactly as both collectors emit them.

    Args:
        rng (random.Random): The seeded generator.
        count (int): How many drives to build.

    Returns:
        tuple[tuple[dict[str, object], ...], int]: The drives and their
            total capacity in bytes.
    """
    drives: list[dict[str, object]] = []
    total = 0
    for i in range(count):
        media = rng.choice(_DRIVE_MEDIA)
        capacity = rng.choice(_DRIVE_CAPACITIES_BYTES[media])
        health = rng.choice(_COMPONENT_HEALTHS)
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


def _build_gpus(rng: random.Random, collector: ManagerType) -> tuple[dict[str, object], ...] | None:
    """
    A server's GPUs, keyed exactly as `redfish.mapping.gpus_from_processors`
    emits them.

    Each collector has a different ceiling here, and reproducing that is
    the point: UCS Manager exposes no GPU objects at all (`None`);
    Intersight exposes a `graphics.Card`'s identity but carries no
    memory, thermal, power or ECC field anywhere in its schema, so those
    are `None` while the GPU itself is real; only Redfish reports the
    telemetry, from `ProcessorMetrics`/`EnvironmentMetrics`.

    Args:
        rng (random.Random): The seeded generator.
        collector (ManagerType): The collector that owns the server.

    Returns:
        tuple[dict[str, object], ...] | None: The GPUs — empty for most
            servers, which is "none discoverable" rather than "none
            installed" — or `None` for UCS Central.
    """
    count = rng.choice(_GPU_COUNTS)
    gpu_vendor, model, memory_type, memory_bytes = rng.choice(_GPU_MODELS)
    if collector is ManagerType.UCS_CENTRAL:
        return None
    if collector is ManagerType.INTERSIGHT:
        return tuple(
            {
                "vendor": gpu_vendor,
                "model": model,
                "serial": f"GPU{rng.getrandbits(32):08x}",
                "memory_bytes": None,
                "health": rng.choice(_COMPONENT_HEALTHS),
                "pci_address": None,
                "firmware_version": None,
                "memory_type": None,
                "ecc_mode_enabled": None,
                "correctable_error_count": None,
                "uncorrectable_error_count": None,
                "temperature_celsius": None,
                "power_watts": None,
            }
            for _ in range(count)
        )
    return tuple(
        {
            "vendor": gpu_vendor,
            "model": model,
            "serial": f"GPU{rng.getrandbits(32):08x}",
            "memory_bytes": memory_bytes,
            "health": rng.choice(_COMPONENT_HEALTHS),
            # Redfish has no PCI address on a `Processor`; the collector
            # reports None rather than inventing one.
            "pci_address": None,
            "firmware_version": f"{rng.randint(535, 560)}.{rng.randint(0, 99):02d}.01",
            "memory_type": memory_type,
            "ecc_mode_enabled": True,
            "correctable_error_count": rng.choice((0, 0, 0, 1, 17)),
            "uncorrectable_error_count": rng.choice((0, 0, 0, 0, 1)),
            "temperature_celsius": float(rng.randint(38, 82)),
            "power_watts": float(rng.randint(90, 700)),
        }
        for _ in range(count)
    )


def _build_attachments(rng: random.Random, *, site_code: str) -> tuple[ProviderAttachment, ...]:
    """
    A Cisco server's fabric attachments.

    The UCS collector reports both the physical uplinks (`adaptorExtEthIf`)
    and the vNICs carved out of them (`adaptorHostEthIf`), telling them
    apart with `interface_kind` — so the fixture does too, or nothing in
    dev ever sees the two kinds together.

    Args:
        rng (random.Random): The seeded generator.
        site_code (str): The site, which names the fabric interconnects.

    Returns:
        tuple[ProviderAttachment, ...]: The physical ports followed by
            their vNICs.
    """
    count = rng.choice(_ATTACHMENT_COUNTS)
    if count == 0:
        return ()

    oper_states = rng.choice(_OPER_STATE_PATTERNS[count])

    physical: list[ProviderAttachment] = []
    for i, oper_state in enumerate(oper_states):
        fabric = "A" if i % 2 == 0 else "B"
        fabric_name = f"FI-{fabric}-{site_code}-01"
        physical.append(
            ProviderAttachment(
                type="FABRIC_INTERCONNECT",
                provider=ManagerType.UCS_CENTRAL.value,
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
                interface_kind="PHYSICAL",
            )
        )

    vnics = [
        replace(
            port,
            server_interface=f"vnic{i}-{n}",
            interface_kind="VNIC",
        )
        for i, port in enumerate(physical)
        for n in range(_VNICS_PER_PORT)
    ]
    return tuple(physical) + tuple(vnics)


def generate_servers(*, seed: int, count: int) -> Iterator[ProviderServer]:
    """
    Yield `count` deterministic `ProviderServer` DTOs for the given `seed`.

    Every random decision is drawn from one `random.Random(seed)` in a
    fixed order, so this is fully reproducible: same `(seed, count)` ->
    field-for-field identical output, every run.

    Args:
        seed (int): The RNG seed.
        count (int): How many servers to generate.

    Yields:
        ProviderServer: One fake server.
    """
    rng = random.Random(seed)  # noqa: S311 - deterministic fake data, never security-sensitive
    sites = _SITE_CODES

    for index in range(count):
        site_index = index % len(sites)
        site_code = sites[site_index]
        # Stepped by site cycle, not by `index`: there are as many vendors
        # as sites, so a plain `index % len(_VENDORS)` locks each site to
        # exactly one vendor and leaves every per-site vendor breakdown a
        # single bar.
        vendor = _VENDORS[(index // len(sites)) % len(_VENDORS)]
        # Shuffle vendor pick slightly so it isn't perfectly periodic —
        # still fully deterministic (drawn from `rng`), just less uniform.
        if rng.random() < 0.15:
            vendor = rng.choice(_VENDORS)

        # `model` and `memory_gib` are drawn before `name` because one of
        # the real hostname shapes embeds both
        # (`ocp-dell-r650-tlv-128c-1024gb-<serial>`).
        model = rng.choice(_MODELS[vendor])
        collector = collector_for(vendor, model)
        memory_gib = rng.choice((128, 256, 512, 1024))
        memory_total_bytes = memory_gib * 1024**3

        name = _build_name(rng, vendor, index, site_code, model, memory_gib)
        serial = f"{vendor[:3].upper()}{index:07d}"
        system_uuid = _random_uuid(rng)

        nic_count = rng.randint(2, 4)
        nic_macs = tuple(_random_mac(rng) for _ in range(nic_count))
        bmc_mac = _random_mac(rng)
        bmc_ip = _fake_ip(site_index, index)

        cpu_sockets = rng.choice((1, 2))
        cpu_cores = rng.choice((16, 24, 32, 48, 64))
        cpu_threads = cpu_cores * 2
        cpu_model = rng.choice(_CPU_MODELS[vendor])

        drive_count = rng.randint(2, 8)
        storage_drives, storage_total_bytes = _build_storage_drives(rng, drive_count)
        gpus = _build_gpus(rng, collector)

        # Only Cisco has a fabric interconnect in front of it — both
        # Cisco collectors report one — while a standalone BMC has nothing
        # to attach, and an empty tuple keeps the seeded
        # `connectivity.fabric_paths_down` policies from evaluating against
        # fiction.
        attachments = _build_attachments(rng, site_code=site_code) if vendor == "cisco" else ()
        template_name, template_external_id = _profile_template(rng, vendor)

        yield ProviderServer(
            external_id=_external_id(collector, index=index, site_index=site_index, bmc_ip=bmc_ip),
            vendor=vendor,
            name=name,
            model=model,
            serial=serial,
            system_uuid=system_uuid,
            nic_macs=nic_macs,
            bmc_address_raw=_bmc_address(collector, bmc_ip),
            bmc_mac=bmc_mac,
            manager_id=manager_id_for(collector),
            profile_dn=_profile_dn(collector, name=name, site_code=site_code),
            profile_template_name=template_name,
            profile_template_external_id=template_external_id,
            cpu_sockets=cpu_sockets,
            cpu_cores=cpu_cores,
            cpu_threads=cpu_threads,
            cpu_model=cpu_model,
            memory_total_bytes=memory_total_bytes,
            storage_total_bytes=storage_total_bytes,
            storage_drives=storage_drives,
            gpus=gpus,
            attachments=attachments,
            # No collector reports tags: UCS's are per-org labels the
            # provider does not read, Intersight's are not mapped, and a
            # BMC has none at all.
            tags=(),
        )
