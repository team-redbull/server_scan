"""`ServerInventoryProvider` implementation backed by deterministic fake
data (`app.infrastructure.providers.fake.generator`).

Exists so the ingestion pipeline (`app.application.services.ingest`) is
exercised end-to-end — normalize -> correlate -> upsert — against the
exact same `ServerInventoryProvider`/`ProviderServer` seam a real Dell/
Cisco/HPE collector will implement later, per the port's own docstring.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.domain.ports.provider import ProviderServer
from app.infrastructure.providers.fake.generator import generate_servers


class FakeProvider:
    """`ServerInventoryProvider` for deterministic fake data. `seed` and
    `count` are fixed at construction time — `list_servers()` yields the
    same `count` servers, generated from the same `seed`, every time it is
    called on a given instance.
    """

    provider_type = "fake"

    def __init__(self, *, seed: int, count: int) -> None:
        self._seed = seed
        self._count = count

    async def health_check(self) -> None:
        """No real backend to check — the fake provider is always healthy."""
        return

    async def list_servers(self) -> AsyncIterator[ProviderServer]:
        for server in generate_servers(seed=self._seed, count=self._count):
            yield server
