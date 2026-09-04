"""`ServerInventoryProvider` for HPE servers — the OneView entry point.

**OneView is the only source for every HPE server, whatever its iLO
generation.** This collector never contacts a BMC, has no BMC
credentials, and has no per-generation branch in its collection path.
That is a deliberate user decision for a mixed iLO 4/5/6 estate — one
collection standard for all HP hardware — and what it costs on Gen10 and
Gen11 is written down in
docs/adr/0022-oneview-only-hpe-collector.md rather than worked around.

**The whole appliance costs a handful of requests.**
`GET /rest/server-hardware` returns the complete object per member, not a
summary, and `expand=all` folds each server's DIMMs, drives and PCI
devices into that same response. So three paginated calls — profiles,
profile templates, expanded hardware — cover everything except power
supplies.

Power supplies are the one genuinely per-server call, and they are worth
it: `IngestService` has never had a provider populate `psus`, while the
health engine has carried `power.psu_count` and `power.failed_psu_count`
the whole time. That pass is bounded by a semaphore and can be switched
off (`INVENTORY_ONEVIEW_COLLECT_PSUS`), because it is the difference
between ~15 requests and ~2500.

One appliance, one endpoint, exactly like every other vendor here — see
docs/adr/0012. The 2500-server-per-appliance ceiling is documented in
docs/hpe-collectors.md, not coded around.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import AsyncIterator, Callable
from typing import Any

import structlog

from app.domain.enums import ManagerType
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.domain.ports.provider import ProviderServer
from app.infrastructure.providers.oneview.client import (
    DEFAULT_PAGE_SIZE,
    EXPANDED_PAGE_SIZE,
    OneViewClient,
)
from app.infrastructure.providers.oneview.mapping import (
    DEVICES,
    POWER_SUPPLIES,
    OneViewProfile,
    ilo_generation,
    profile_from,
    server_from,
    subresource,
    subresource_data,
)

logger = structlog.get_logger(__name__)

_PROVIDER_TYPE = ManagerType.ONEVIEW.value

_SERVER_PROFILES = "/rest/server-profiles"
_SERVER_PROFILE_TEMPLATES = "/rest/server-profile-templates"
_SERVER_HARDWARE = "/rest/server-hardware"


class OneViewProvider:
    """
    Collects one HPE OneView appliance's inventory.

    See docs/adr/0022-oneview-only-hpe-collector.md.
    """

    provider_type = _PROVIDER_TYPE

    def __init__(
        self,
        *,
        manager: Manager,
        credentials: ManagerConnection,
        timeout_seconds: float,
        name_pattern: str = "",
        page_size: int = DEFAULT_PAGE_SIZE,
        collect_psus: bool = True,
        psu_concurrency: int = 8,
        api_version: int = 0,
        verify_tls: bool = False,
        client_factory: Callable[[], OneViewClient] | None = None,
    ) -> None:
        """
        Bind a provider to one appliance.

        Args:
            manager (Manager): The manager this run reports under. Its
                `id` becomes each server's `manager_id`; its `endpoint`
                is the appliance.
            credentials (ManagerConnection): The OneView login
                (`INVENTORY_ONEVIEW_IP`/`_USERNAME`/`_PASSWORD`).
            timeout_seconds (float): Per-request timeout.
            name_pattern (str): Regex; a profile whose name does not
                match is dropped before its hardware is mapped and before
                its power supplies are fetched. Empty collects
                everything. Only an efficiency gate — the authoritative
                filter is `tools.run_collector`'s wrapper.
            page_size (int): Explicit `count` for the plain collection
                GETs. Never `-1`, which means 64 on the profiles
                resource.
            collect_psus (bool): Whether to make the per-server
                `/powerSupplies` call for servers the expanded payload
                did not already cover. Off turns a ~2500-request run
                back into a ~15-request one, at the cost of every
                server's `psus` being unread.
            psu_concurrency (int): How many of those calls run at once.
            api_version (int): `X-Api-Version` override; `0` discovers
                and clamps it.
            verify_tls (bool): Whether to verify the appliance's TLS
                certificate.
            client_factory (Callable[[], OneViewClient] | None): Builds
                the client. Injected so a test can substitute the
                transport; `None` builds the real one.

        Raises:
            ValueError: If `manager` has no endpoint configured.
        """
        if not manager.endpoint:
            raise ValueError(f"Manager {manager.id!r} has no endpoint configured.")
        self._endpoint: str = manager.endpoint
        self._manager = manager
        self._credentials = credentials
        self._timeout_seconds = timeout_seconds
        self._pattern = re.compile(name_pattern) if name_pattern else None
        self._page_size = page_size
        self._collect_psus = collect_psus
        self._psu_concurrency = max(1, psu_concurrency)
        self._api_version = api_version
        self._verify_tls = verify_tls
        self._client_factory = client_factory or self._new_client

    def _new_client(self) -> OneViewClient:
        """
        Build a client for this appliance. Sessions are never shared.

        Returns:
            OneViewClient: A fresh, not-yet-logged-in client.
        """
        return OneViewClient(
            endpoint=self._endpoint,
            username=self._credentials.username,
            password=self._credentials.password,
            timeout_seconds=self._timeout_seconds,
            api_version=self._api_version,
            verify_tls=self._verify_tls,
        )

    async def health_check(self) -> None:
        """
        Verify the appliance is reachable and the credentials accepted.

        Raises:
            OneViewConnectionError: If version discovery, login or the
                connection fails.
        """
        async with self._client_factory():
            return

    async def list_servers(self) -> AsyncIterator[ProviderServer]:
        """
        Yield every matched HPE server on the appliance.

        Yields:
            ProviderServer: One HPE server, named by its server profile
                and measured by OneView.

        Raises:
            OneViewConnectionError: On login failure or a failure of any
                bulk enumeration call. There is one appliance, so its
                failure is the run's failure.
        """
        async with self._client_factory() as client:
            profiles = await client.get_all(_SERVER_PROFILES, page_size=self._page_size)
            templates = await client.get_all(_SERVER_PROFILE_TEMPLATES, page_size=self._page_size)
            # `expand=all` inlines every server's subresource data — the
            # DIMMs, drives and PCI devices — in the list response, which
            # is the difference between three calls for the appliance and
            # three per server. Paged small because that payload is why
            # HPE leaves `expand` off by default.
            hardware = await client.get_all(
                _SERVER_HARDWARE, page_size=EXPANDED_PAGE_SIZE, params={"expand": "all"}
            )
            matched = self._matched(profiles=profiles, templates=templates, hardware=hardware)
            power_supplies = await self._power_supplies(client, [member for member, _ in matched])

        logger.info(
            "oneview.collected",
            endpoint=self._endpoint,
            profiles=len(profiles),
            hardware=len(hardware),
            servers=len(matched),
        )
        for member, profile in matched:
            yield server_from(
                hardware=member,
                profile=profile,
                manager_id=self._manager.id,
                power_supplies=power_supplies.get(str(member.get("uri") or "")),
            )

    def _matched(
        self,
        *,
        profiles: list[dict[str, Any]],
        templates: list[dict[str, Any]],
        hardware: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], OneViewProfile]]:
        """
        Join hardware to profiles in memory and keep what this run collects.

        Args:
            profiles (list[dict[str, Any]]): `/rest/server-profiles`.
            templates (list[dict[str, Any]]): profile templates.
            hardware (list[dict[str, Any]]): `/rest/server-hardware`.

        Returns:
            list[tuple[dict[str, Any], OneViewProfile]]: Each matched
                server-hardware member with the profile that names it.
        """
        template_names = {
            uri: name
            for uri, name in (
                (str(t.get("uri") or ""), str(t.get("name") or "")) for t in templates
            )
            if uri and name
        }
        by_uri: dict[str, OneViewProfile] = {}
        for raw in profiles:
            parsed = profile_from(raw, template_names=template_names)
            if parsed is not None:
                by_uri[parsed.uri] = parsed

        matched: list[tuple[dict[str, Any], OneViewProfile]] = []
        unassigned = 0
        filtered = 0
        unreadable: Counter[str] = Counter()
        for member in hardware:
            profile = by_uri.get(str(member.get("serverProfileUri") or ""))
            if profile is None:
                # No profile means no operator-assigned name: OneView's
                # own `name` is a bay location or `ILO<serial>`, which
                # carries no site token and matches no classification
                # rule, so such a server is skipped rather than ingested
                # under a name nothing downstream can use. An unassigned
                # server is by definition carrying no workload.
                unassigned += 1
                continue
            if self._pattern is not None and not self._pattern.search(profile.name):
                filtered += 1
                continue
            self._count_unreadable(member, unreadable)
            matched.append((member, profile))

        if unassigned:
            logger.info(
                "oneview.hardware_without_profile",
                endpoint=self._endpoint,
                servers=unassigned,
                hint=(
                    "Server hardware with no assigned server profile has no "
                    "operator-assigned name and was not collected."
                ),
            )
        if filtered:
            logger.info("oneview.profiles_filtered", endpoint=self._endpoint, servers=filtered)
        if unreadable:
            # One aggregated line, not one per host: on a mixed estate
            # every iLO-4 server answers `InsufficientFirmware` for every
            # subresource, and a per-host line would bury the run's real
            # output.
            logger.warning(
                "oneview.subresources_unreadable",
                endpoint=self._endpoint,
                servers=sum(unreadable.values()),
                by_state_and_generation=dict(unreadable),
                hint=(
                    "These servers report drives and PCI devices as unread (None), not "
                    "as zero — the stored values are carried forward. iLO 4 cannot "
                    "report any subresource; HPE's documented minimum is iLO 5 v1.20."
                ),
            )
        return matched

    async def _power_supplies(
        self, client: OneViewClient, hardware: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Collect each matched server's power supplies, the cheap way first.

        `/powerSupplies` returns a `SubResourceV10` envelope but has no
        matching `SubResourceName` value, so whether `expand=all` already
        included it is undetermined in HPE's own documentation. Any
        server whose expanded payload carried it costs nothing; only the
        rest are fetched, under a semaphore, and only when
        `INVENTORY_ONEVIEW_COLLECT_PSUS` is on.

        Which path ran is logged with counts, because the difference
        between them is ~15 requests and ~2500 against an appliance whose
        rate limits HPE does not document at all.

        Args:
            client (OneViewClient): The logged-in client.
            hardware (list[dict[str, Any]]): The matched members.

        Returns:
            dict[str, list[dict[str, Any]]]: Server-hardware URI -> its
                `PowerSupplies` rows. A URI absent from this map is
                reported as unread, never as no power supplies.
        """
        collected: dict[str, list[dict[str, Any]]] = {}
        to_fetch: list[str] = []
        for member in hardware:
            uri = str(member.get("uri") or "")
            rows = subresource_data(member, POWER_SUPPLIES)
            if rows is None:
                to_fetch.append(uri)
            else:
                collected[uri] = rows

        logger.info(
            "oneview.power_supply_source",
            endpoint=self._endpoint,
            from_expand=len(collected),
            per_server_calls=len(to_fetch) if self._collect_psus else 0,
            collect_psus=self._collect_psus,
        )
        if not self._collect_psus or not to_fetch:
            return collected

        semaphore = asyncio.Semaphore(self._psu_concurrency)

        async def fetch(uri: str) -> tuple[str, list[dict[str, Any]] | None]:
            """
            Fetch one server's power supplies, containing its failure.

            Args:
                uri (str): The server-hardware URI.

            Returns:
                tuple[str, list[dict[str, Any]] | None]: The URI and its
                    rows, or `None` if the call failed or reported a
                    state other than `Collected`.
            """
            async with semaphore:
                try:
                    body = await client.get_json(f"{uri}/powerSupplies")
                except Exception:
                    return uri, None
            data = body.get("data")
            rows = data.get("Members") if isinstance(data, dict) else data
            if body.get("collectionState") != "Collected" or not isinstance(rows, list):
                return uri, None
            return uri, [row for row in rows if isinstance(row, dict)]

        failures = 0
        for uri, rows in await asyncio.gather(*(fetch(uri) for uri in to_fetch)):
            if rows is None:
                failures += 1
            else:
                collected[uri] = rows
        if failures:
            # Aggregated, and not fatal: a server whose PSUs could not be
            # read reports `psus=None`, which ingest carries forward.
            logger.warning(
                "oneview.power_supplies_unreadable",
                endpoint=self._endpoint,
                servers=failures,
                of=len(to_fetch),
            )
        return collected

    @staticmethod
    def _count_unreadable(member: dict[str, Any], tally: Counter[str]) -> None:
        """
        Record one server whose subresources could not be read.

        Keyed by `<collectionState>/iLO<generation>` so one line answers
        both "why" and "which hardware" — the two questions an operator
        seeing unread inventory actually has.

        Args:
            member (dict[str, Any]): One `/rest/server-hardware` member.
            tally (Counter[str]): The running tally.
        """
        envelope = subresource(member, DEVICES)
        state = str(envelope.get("collectionState") or "absent")
        if state == "Collected":
            return
        generation = ilo_generation(member.get("mpModel"))
        tally[f"{state}/iLO{generation if generation is not None else '?'}"] += 1
