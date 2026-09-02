"""A deployment-supplied lookup from a GPU's PID to its known VRAM.

**Fills a gap neither Cisco management plane closes on its own.**
Intersight's `graphics.Card` and UCS Manager's `graphicsCard` both report
a GPU's identity (model/vendor/serial/PID) but neither reports memory
size or power draw anywhere — confirmed against both SDKs' full field
sets and, for Intersight, Cisco's own official metrics API too. See
docs/cisco-collectors.md, "GPUs (coprocessor cards vs. graphics cards)".

What both *do* report is the card's PID — Cisco's own part-number scheme
(e.g. `P1001-200`), stable per SKU regardless of vendor firmware version.
This module lets a deployment tell the platform what a PID it recognizes
actually is, the same "deployment knowledge, not code" shape
`app.domain.value_objects.site` already uses for `INVENTORY_SITES` — see
docs/adr/0018-sites-from-configuration.md for the precedent this follows.

**Deliberately not a hardcoded table in this repo.** A PID-to-SKU mapping
is operator knowledge (Cisco's own spec sheets), changes as new GPU
models ship, and — like `INVENTORY_SITES` — this codebase should not
assert it as a fact frozen at whatever moment this file was last edited.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


class GpuCatalogConfigurationError(ValueError):
    """`INVENTORY_GPU_MODELS` could not be read.

    Raised at startup, never during a request: a typo here silently
    disables enrichment for the PID it was meant to cover, which is far
    harder to notice than a startup failure.
    """


@dataclass(frozen=True, slots=True)
class GpuModelDefinition:
    """
    One known GPU SKU.

    Attributes:
        pid (str): The Cisco PID this deployment recognizes, e.g.
            `"P1001-200"`. Matched case-insensitively — see
            `GpuCatalog.enrich`.
        name (str): The friendly name to report in place of the bare PID,
            e.g. `"NVIDIA A100 40GB"`.
        memory_bytes (int): The card's known VRAM, in bytes.
    """

    pid: str
    name: str
    memory_bytes: int


@dataclass(frozen=True, slots=True)
class GpuCatalog:
    """
    The GPU PIDs this deployment can enrich, keyed by normalized PID.

    Closed at runtime, contents come from `INVENTORY_GPU_MODELS`.
    Immutable once built, same reasoning as `SiteCatalog`: it can be
    shared freely and cannot drift mid-run.
    """

    definitions: tuple[GpuModelDefinition, ...]

    @classmethod
    def from_spec(cls, spec: str) -> GpuCatalog:
        """
        Parse `INVENTORY_GPU_MODELS` into a catalog.

        The format is `PID:Friendly Name:VRAM_GB`, comma-separated:

            P1001-200:NVIDIA A100 40GB:40,P1010-200:NVIDIA H100 80GB:80

        Unlike `INVENTORY_SITES`, there is no shipped default — a PID
        this codebase has not been told about enriches nothing, which is
        the correct behavior for an unconfigured deployment, not a gap
        to paper over with a guessed table.

        Args:
            spec (str): The raw configured value. Empty means an empty
                catalog, not an error — this feature is opt-in.

        Returns:
            GpuCatalog: The parsed catalog, in configured order.

        Raises:
            GpuCatalogConfigurationError: On a malformed or duplicate
                entry.
        """
        text = spec.strip()
        if not text:
            return cls(definitions=())

        definitions: list[GpuModelDefinition] = []
        seen: set[str] = set()
        for entry in text.split(","):
            if not entry.strip():
                continue
            parts = entry.split(":")
            if len(parts) != 3:
                raise GpuCatalogConfigurationError(
                    f"INVENTORY_GPU_MODELS: {entry.strip()!r} is not in the required "
                    "'PID:Friendly Name:VRAM_GB' shape — expected exactly two ':' "
                    "separators, e.g. 'P1001-200:NVIDIA A100 40GB:40'."
                )
            pid, name, vram_gb_raw = (part.strip() for part in parts)
            if not pid:
                raise GpuCatalogConfigurationError(
                    "INVENTORY_GPU_MODELS: an entry names no PID — the part before the first ':'."
                )
            if not name:
                raise GpuCatalogConfigurationError(
                    f"INVENTORY_GPU_MODELS: PID {pid!r} names no friendly name — unlike "
                    "INVENTORY_SITES, there is no way to derive one from the PID alone."
                )
            try:
                vram_gb = int(vram_gb_raw)
            except ValueError as exc:
                raise GpuCatalogConfigurationError(
                    f"INVENTORY_GPU_MODELS: PID {pid!r}'s VRAM {vram_gb_raw!r} is not a "
                    "whole number of GB."
                ) from exc
            if vram_gb <= 0:
                raise GpuCatalogConfigurationError(
                    f"INVENTORY_GPU_MODELS: PID {pid!r}'s VRAM must be positive, got {vram_gb}."
                )
            key = pid.upper()
            if key in seen:
                raise GpuCatalogConfigurationError(
                    f"INVENTORY_GPU_MODELS: PID {pid!r} is listed twice."
                )
            seen.add(key)
            definitions.append(
                GpuModelDefinition(pid=pid, name=name, memory_bytes=vram_gb * 1024**3)
            )
        return cls(definitions=tuple(definitions))

    def _for_pid(self, pid: str) -> GpuModelDefinition | None:
        """
        Look up one PID, case-insensitively.

        Args:
            pid (str): The PID as a provider reported it.

        Returns:
            GpuModelDefinition | None: The matching definition, or `None`.
        """
        key = pid.strip().upper()
        for definition in self.definitions:
            if definition.pid.upper() == key:
                return definition
        return None

    def enrich(self, gpu: Mapping[str, Any]) -> dict[str, Any]:
        """
        Fill in a GPU's memory from this catalog, when the API left it
        unknown and this deployment recognizes the PID.

        Real data always wins: a GPU whose `memory_bytes` a collector
        already populated (no vendor does today, but the mapping's
        contract does not rule it out for tomorrow) is returned
        unchanged — this catalog only fills a gap, it never overrides a
        vendor's own answer, matching the platform-wide "a provider's
        `None` means unread, not zero" contract
        (`app.domain.ports.provider.ProviderServer`).

        Args:
            gpu (Mapping[str, Any]): One entry from `ProviderServer.gpus`
                — keys mirror `app.domain.models.hardware.Gpu`.

        Returns:
            dict[str, Any]: `gpu` unchanged, or a copy with `model`
                replaced by the friendly name and `memory_bytes` filled
                in, when `memory_bytes` was `None` and `model` matches a
                configured PID.
        """
        if gpu.get("memory_bytes") is not None:
            return dict(gpu)
        model = gpu.get("model")
        if not isinstance(model, str):
            return dict(gpu)
        definition = self._for_pid(model)
        if definition is None:
            return dict(gpu)
        enriched = dict(gpu)
        enriched["model"] = definition.name
        enriched["memory_bytes"] = definition.memory_bytes
        return enriched


@lru_cache(maxsize=8)
def gpu_catalog(spec: str) -> GpuCatalog:
    """
    A cached catalog for one configured spec.

    Cached for the same reason `site_catalog` is: built once per unique
    spec rather than re-parsed on every server. Keyed on the spec itself,
    not on `Settings`, so a test can pass a literal.

    Args:
        spec (str): The `INVENTORY_GPU_MODELS` value.

    Returns:
        GpuCatalog: The parsed catalog.

    Raises:
        GpuCatalogConfigurationError: On a malformed entry.
    """
    return GpuCatalog.from_spec(spec)
