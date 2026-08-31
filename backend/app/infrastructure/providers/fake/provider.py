"""`ServerInventoryProvider` implementation backed by deterministic fake
data (`app.infrastructure.providers.fake.generator`).

Exists so the ingestion pipeline (`app.application.services.ingest`) is
exercised end-to-end — normalize -> correlate -> upsert — against the
exact same `ServerInventoryProvider`/`ProviderServer` seam the real
collectors implement.

One instance stands in for one collector: `provider_type` both names what
`Server.source_provider` is stamped with and selects the fake servers that
collector would own, so a seeded fleet carries the same `source_provider`
values a really-collected one does. `fake_providers()` builds the full
set.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.domain.ports.provider import ProviderServer
from app.domain.value_objects.site import SiteCatalog
from app.infrastructure.providers.fake.generator import (
    COLLECTOR_TYPES,
    generate_servers,
    provider_type_for,
)


class FakeProvider:
    """`ServerInventoryProvider` for deterministic fake data. `seed`,
    `count` and `provider_type` are fixed at construction time —
    `list_servers()` yields the same servers, generated from the same
    `seed`, every time it is called on a given instance.
    """

    def __init__(
        self,
        *,
        seed: int,
        count: int,
        provider_type: str,
        sites: SiteCatalog | None = None,
    ) -> None:
        """
        Args:
            seed (int): The generator seed.
            count (int): How many servers the whole fake fleet holds — not
                how many this provider yields, which is the subset this
                collector would have found.
            provider_type (str): The collector this instance imitates, a
                `ManagerType` value.
            sites (SiteCatalog | None): The sites whose codes appear in
                generated hostnames, or None for the shipped default.
        """
        self._seed = seed
        self._count = count
        self.provider_type = provider_type
        self._sites = sites

    async def health_check(self) -> None:
        """No real backend to check — the fake provider is always healthy."""
        return

    async def list_servers(self) -> AsyncIterator[ProviderServer]:
        """
        Yields:
            ProviderServer: Each fake server this collector would own.
        """
        for server in generate_servers(seed=self._seed, count=self._count, sites=self._sites):
            if provider_type_for(server) == self.provider_type:
                yield server


def fake_providers(
    *, seed: int, count: int, sites: SiteCatalog | None = None
) -> list[FakeProvider]:
    """
    One provider per collector the platform actually runs.

    Together they yield the whole fake fleet exactly once, each server
    behind the collector that would really have found it.

    Args:
        seed (int): The generator seed.
        count (int): How many servers the fleet holds in total.
        sites (SiteCatalog | None): The sites whose codes appear in
            generated hostnames, or None for the shipped default.

    Returns:
        list[FakeProvider]: A provider per implemented collector.
    """
    return [
        FakeProvider(seed=seed, count=count, provider_type=manager_type.value, sites=sites)
        for manager_type in COLLECTOR_TYPES
    ]
