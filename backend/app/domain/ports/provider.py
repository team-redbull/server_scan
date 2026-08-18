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
    nic_macs: tuple[str, ...] = ()

    bmc_address_raw: str | None = None
    bmc_mac: str | None = None

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

    cpu_sockets: int = 0
    cpu_cores: int = 0
    cpu_threads: int = 0
    cpu_model: str | None = None

    memory_total_bytes: int = 0

    storage_total_bytes: int = 0
    storage_drives: tuple[dict[str, object], ...] = ()

    attachments: tuple[ProviderAttachment, ...] = ()

    tags: tuple[str, ...] = field(default_factory=tuple)


class ServerInventoryProvider(Protocol):
    provider_type: str

    async def health_check(self) -> None: ...

    def list_servers(self) -> AsyncIterator[ProviderServer]: ...
