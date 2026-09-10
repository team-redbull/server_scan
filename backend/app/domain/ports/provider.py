"""The collector seam every vendor provider implements.

`ServerInventoryProvider` and `ProviderServer` are the interface all seven
providers (`fake`, `ucs_manager`, `ucs_central`, `intersight`,
`openmanage`, `oneview`, `redfish`) implement/produce.
`app.application.services.ingest` is the one caller: it drives
`collect()`, then normalizes each `ProviderServer` into a domain `Server`
(correlate -> classify -> health-evaluate -> upsert).

`ProviderServer` is intentionally flatter and less structured than the
domain `Server` model: it's the raw-ish shape a collector naturally
produces (already vendor-normalized, but not yet correlated against
existing records or run through the search/cursor/health/classification
machinery).

`ServerInventoryProvider` is an `ABC`, not a `Protocol` — see ADR-0023 for
why, and for the mechanism behind `_list_servers`' exact signature.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProviderAttachment:
    """Provider-neutral DTO for one fabric/uplink connection a server reports."""

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
    """
    Provider-neutral DTO for one server as reported by a collector (real or fake).

    All identity/MAC/BMC-address values are already normalized by the
    provider before this DTO is constructed — the provider boundary is
    where vendor-specific parsing happens; nothing downstream re-parses
    vendor formats.
    """

    external_id: str
    vendor: str
    name: str
    model: str | None = None

    serial: str | None = None
    system_uuid: str | None = None

    # True unless a provider knows a server's identity but could not reach
    # it this run — see `Server.reachable`. Every field below stays `None`
    # on such a record, so nothing is blanked.
    reachable: bool = True

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

    # Keys mirror `app.domain.models.hardware.MemoryModule`. The same shape
    # of gap `psus` was: the domain model has had `Memory.modules` since
    # the first slice and `IngestService` hardcoded `modules=[]`, so a
    # degraded DIMM was unrepresentable. Redfish already reads the `Memory`
    # collection for the total, so populating this costs no extra request.
    memory_modules: tuple[dict[str, object], ...] | None = None

    attachments: tuple[ProviderAttachment, ...] = ()

    tags: tuple[str, ...] = field(default_factory=tuple)


class ServerInventoryProvider(ABC):
    """
    The lifecycle every vendor collector implements.

    `collect()` is the one method a caller drives: it resets
    `collection_errors` for this run and guarantees `_list_servers()`'s
    generator is closed even if the caller stops early (a raised
    exception, `--limit`, a cancelled task), which releases whatever
    session or resources it opened rather than deferring that to
    garbage collection. A subclass fills in `_list_servers()`,
    `health_check()`, and calls `_record_error()` for a failure that
    means part of the fleet was not collected — see ADR-0023 for what
    does and does not count as one.

    Subclasses with their own `__init__` must call `super().__init__()`.
    """

    provider_type: str

    def __init__(self) -> None:
        """Start this run's failure list empty."""
        self._collection_errors: list[str] = []

    @property
    def collection_errors(self) -> tuple[str, ...]:
        """
        Partial failures this run recorded.

        Read by `tools.run_collector`, which turns a non-empty result into
        exit code 3 (PARTIAL) — a run that reported success despite
        missing part of the fleet is worse than one that says so.

        Returns:
            tuple[str, ...]: One message per failure, empty for a run
                that collected everything it planned to.
        """
        return tuple(self._collection_errors)

    def _record_error(self, message: str) -> None:
        """
        Record one failure that cost this run part of the fleet.

        Args:
            message (str): A human-readable description, naming the
                endpoint/domain/host and, where relevant, the numbers
                (e.g. how many were expected versus fetched) — this is
                what an operator reads to tell a lost connection from a
                paging ceiling.
        """
        self._collection_errors.append(message)

    async def collect(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Run this collector once, yielding every server it can reach.

        Declared `AsyncGenerator`, not the narrower `AsyncIterator`, so a
        caller that closes it early (`generator.aclose()`, or wrapping it
        in another `contextlib.aclosing`) type-checks — matching why
        `_list_servers` is declared the same way.

        Resets `collection_errors` before iterating, so a second call on
        the same instance reports only this run's failures. Wraps
        `_list_servers()` in `contextlib.aclosing` so an abandoned run
        still closes the underlying generator — and with it, whatever
        session or client `_list_servers()` opened.

        Yields:
            ProviderServer: Each server `_list_servers()` produces.
        """
        self._collection_errors = []
        async with aclosing(self._list_servers()) as stream:
            async for server in stream:
                yield server

    @abstractmethod
    async def health_check(self) -> None:
        """
        Verify this collector is reachable and its credentials work.

        Raises:
            Exception: A vendor-specific connection or authentication
                error, on any failure.
        """
        ...

    @abstractmethod
    def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Yield every server this collector can reach, one pass.

        Must be a plain `def`, never `async def`: an `async def` stub
        with no `yield` in its body types as
        `Coroutine[Any, Any, AsyncGenerator[ProviderServer, None]]`,
        which every concrete async-generator override (every real
        implementation of this method) then violates under Liskov
        substitution — `ty` rejects it on all seven providers. Declared
        `AsyncGenerator`, not the narrower `AsyncIterator`, because
        `collect()` wraps it in `contextlib.aclosing`, which needs
        `aclose()` on the thing it wraps. See ADR-0023.

        Yields:
            ProviderServer: One server, already vendor-normalized.
        """
        ...
