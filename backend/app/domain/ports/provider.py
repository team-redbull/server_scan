"""The future-collector seam.

`ServerInventoryProvider` and `ProviderServer` are the interface every
real vendor collector (Dell OpenManage, Cisco UCS Manager, Cisco
Intersight, HPE OneView) will implement/produce later. The Phase 1 fake
data generator implements this *same* Protocol and emits this *same* DTO
— it does not take a shortcut and write `Server` documents directly — so
the ingestion pipeline (normalize -> correlate -> upsert) is exercised
end-to-end by fake data exactly as it will be by real collectors, and
adding a real collector later is "write a new provider", not "extend the
pipeline".

`ProviderServer` is intentionally flatter and less structured than the
domain `Server` model: it's the raw-ish shape a collector naturally
produces (already vendor-normalized, but not yet correlated against
existing records or run through the search/cursor/health/classification
machinery). `app.application.services.ingest` is what turns one into the
other.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProviderAttachment:
    type: str
    provider: str | None
    fabric: str | None
    fabric_name: str | None
    fabric_id: str | None
    fabric_model: str | None
    fabric_serial: str | None
    server_interface: str | None
    server_port: str | None
    fabric_port: str | None
    admin_state: str
    oper_state: str
    speed_mbps: int | None

    # "PHYSICAL" for an actual cabled uplink (e.g. Cisco's adaptorExtEthIf),
    # "VNIC" for an OS-facing virtual NIC carved out of one (adaptorHostEthIf)
    # — the two can both report the same `fabric`, so a server's physical
    # port count is not derivable from `len(attachments)` without this.
    # Defaults to "PHYSICAL" so providers that don't distinguish (the fake
    # generator) need no change.
    interface_kind: str = "PHYSICAL"


@dataclass(frozen=True, slots=True)
class ProviderNic:
    """Provider-neutral DTO for one host network interface.

    Distinct from `ProviderAttachment`: an attachment is a link to a fabric
    the server hangs off (a UCS fabric interconnect), whereas this is a NIC
    on the server itself as an OS would see it. `link_state` is a plain
    string in the closed set `LinkState` uses ("UP"/"DOWN"/"DISABLED"/
    "UNKNOWN"), kept as a string here for the same reason `ProviderAttachment`
    keeps `oper_state` a string — the provider boundary stays free of domain
    enums, and ingest maps it onto `app.domain.enums.LinkState`.
    """

    name: str
    mac: str | None
    speed_mbps: int | None
    link_state: str

    # Where the NIC physically is, as its own BMC identifies it. The raw
    # identifier by default (iDRAC's FQDD, `NIC.Integrated.1-1-1`), which
    # a vendor-specific collector may rewrite into that vendor's readable
    # form — the Dell collector renders it `controller/port/partition`,
    # `1/1/1`. `None` when the BMC reports nothing to place the NIC by.
    location: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderServer:
    """Provider-neutral DTO for one server as reported by a collector
    (real or fake). All identity/MAC/BMC-address values are already
    normalized by the provider before this DTO is constructed — the
    provider boundary is where vendor-specific parsing happens; nothing
    downstream re-parses vendor formats.
    """

    external_id: str
    vendor: str
    name: str
    model: str | None = None

    serial: str | None = None
    system_uuid: str | None = None

    # `None` means "this collector could not read it on this run", and is
    # NOT the same as an empty tuple / zero, which mean "read, and there
    # are none". `IngestService` carries the previous value forward for a
    # `None`, and overwrites for a real value.
    #
    # Without the distinction a provider whose sub-resource query failed
    # (a Redfish `Storage` collection returning 404, say) reports zeros
    # that overwrite good data — which silently clears the seeded
    # `storage.failed_drive` policy, because zero drives means zero
    # failed drives. See docs/adr/0016-redfish-standalone-collector.md.
    nic_macs: tuple[str, ...] | None = None

    bmc_address_raw: str | None = None
    bmc_mac: str | None = None

    # Per-NIC detail (name, MAC, speed, link up/down). `nic_macs` above is
    # the flat MAC set identity correlation keys on and stays the minimum a
    # provider must supply; `nics` is the richer per-interface view a
    # provider fills in when it has one, and is what populates
    # `NetworkInfo.interfaces`. Empty when a provider reports only MACs.
    nics: tuple[ProviderNic, ...] = ()

    # No `site_id`: a provider does not get to declare a server's site.
    # It is derived from the server's own name at ingest
    # (`app.domain.value_objects.site.parse_site_code`), because a
    # misconfigured manager would otherwise mislabel every server it
    # collects with nothing downstream able to tell.
    manager_id: str | None = None

    # The service/deployment profile's own identity — UCS Manager's DN,
    # which doubles as its org path (e.g. `org-root/org-five/ls-worker-01`).
    # Distinct from `profile_template_*` below: this is the one instance
    # bound to this server, not the reusable template it was created from.
    # Not currently persisted past the dry-run print — see
    # docs/cisco-collectors.md if a vendor other than Cisco populates this.
    profile_dn: str | None = None

    # The reusable profile/deployment template this server's configuration
    # came from — UCS Manager's Service Profile Template, Intersight's
    # Server Profile Template, OneView's Server Profile Template, or an
    # OME Deployment Template. See `app.domain.models.server.
    # ProfileTemplate`'s docstring for the exact per-vendor mapping.
    profile_template_name: str | None = None
    profile_template_external_id: str | None = None

    # `None` throughout means "not read this run" — see `nic_macs` above.
    cpu_sockets: int | None = None
    cpu_cores: int | None = None
    cpu_threads: int | None = None
    cpu_model: str | None = None

    memory_total_bytes: int | None = None

    storage_total_bytes: int | None = None
    storage_drives: tuple[dict[str, object], ...] | None = None

    # Keys mirror `app.domain.models.hardware.Gpu`. `memory_bytes` is
    # already converted: Redfish reports GPU memory in MiB while system
    # memory is GiB, and the port boundary is where vendor units are
    # normalized. An empty tuple means "none discoverable through this
    # provider", which is not the same claim as "none installed" — no
    # standard path is populated by every vendor.
    gpus: tuple[dict[str, object], ...] | None = None

    # Keys mirror `app.domain.models.hardware.Psu`. Added 2026-09-01: the
    # domain model and the health engine's `power.psu_count`/
    # `power.failed_psu_count` metrics already existed, but no provider
    # had ever populated this field — `IngestService` hardcoded
    # `Power(psus=[])`. A server whose PSU is down reported HEALTHY on
    # power the same way a server with two good PSUs did.
    psus: tuple[dict[str, object], ...] | None = None

    attachments: tuple[ProviderAttachment, ...] = ()

    tags: tuple[str, ...] = field(default_factory=tuple)


class ServerInventoryProvider(Protocol):
    provider_type: str

    async def health_check(self) -> None: ...

    def list_servers(self) -> AsyncIterator[ProviderServer]: ...
