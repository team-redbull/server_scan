"""A lookup from a GPU's PID or model string to its known VRAM.

**Fills a gap no management plane this platform collects from closes on
its own.** Intersight's `graphics.Card` and UCS Manager's `graphicsCard`
both report a GPU's identity (model/vendor/serial/PID) but neither
reports memory size or power draw anywhere — confirmed against both
SDKs' full field sets and, for Intersight, Cisco's own official metrics
API too. See docs/cisco-collectors.md, "GPUs (coprocessor cards vs.
graphics cards)". A Redfish-sourced GPU frequently reports no memory
summary either.

Two kinds of identifier reach this catalog, because two kinds of
management plane feed it. Cisco reports a **PID** — its own part-number
scheme (`UCSC-GPU-A100`), stable per SKU regardless of firmware version.
Dell's iDRAC and HPE's iLO report no Cisco PID at all; they report a
**model string** (`NVIDIA A100-PCIE-40GB`, `NVIDIA H100 80GB HBM3`).
Either matches, after normalization — see `GpuCatalog.enrich`.

**This module ships a default table** (`gpu_models.DEFAULT_GPU_MODELS`),
which reverses the original decision to ship none. That decision assumed
a Cisco-only fleet, where the identifier really was operator knowledge
about their own part numbers; a vendor's own model string is not, and an
estate should recognize an A100 without being told what one is. The
operator half is retained where it still earns its place:
`INVENTORY_GPU_MODELS` **overrides** the built-in table rather than
replacing it, so a deployment can correct a row or add a card this
codebase has never heard of, and its answer always wins. See
docs/adr/0021-built-in-gpu-catalog-with-model-matching.md.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.domain.value_objects.gpu_models import DEFAULT_GPU_MODELS

_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")

_VENDOR_PREFIXES = frozenset({"HPE", "HP", "NVIDIA", "AMD", "INTEL", "TESLA", "QUADRO"})

# Marketing nouns that trail a rebranded SKU string and carry no
# identity. HPE reports a GPU as its own product name — the shape
# "HPE NVIDIA A100 40GB PCIe Accelerator" — where Cisco reports a bare
# PID and a BMC reports the chip's own model. Dropped from the end only,
# and the result is still compared for equality, so this can never widen
# a match beyond a spelling of the same card.
_TRAILING_NOISE = frozenset(
    {
        "ACCELERATOR",
        "ACCELERATORS",
        "COMPUTATIONAL",
        "GRAPHICS",
        "MODULE",
        "ADAPTER",
        "ADPTR",
        "CARD",
        "KIT",
        "GPU",
    }
)


def _normalize(identifier: str) -> str:
    """
    Reduce a PID or model string to the key both sides of a match share.

    Uppercases, drops leading vendor and brand words and trailing
    marketing nouns, and removes every separator, so the spellings
    vendors actually use for one card (`A100-PCIE-40GB`,
    `A100 PCIe 40GB`, `NVIDIA A100 PCIe 40GB`,
    `HPE NVIDIA A100 40GB PCIe Accelerator`) collapse to a single key.
    Deliberately nothing more: the result is compared for equality, never
    as a substring, so `A10` can never match `A100`.

    Args:
        identifier (str): A Cisco PID or a vendor-reported model string.

    Returns:
        str: The normalized key, or `""` for a string that is nothing but
            vendor words and punctuation — which matches nothing.
    """
    return "".join(_words(identifier))


def _words(identifier: str) -> list[str]:
    """
    Split an identifier into its meaningful words.

    Uppercases, splits on every separator, then drops leading vendor and
    brand words and trailing marketing nouns. Kept separate from
    `_normalize` because `GpuCatalog._for_identifier` needs the words
    themselves: `"T4 16GB"` and `"T416GB"` join to the same string, and
    only the word list can tell `T4` + `16GB` from `T` + `416GB`.

    Args:
        identifier (str): A Cisco PID or a vendor-reported model string.

    Returns:
        list[str]: The remaining words, in order.
    """
    words = [word for word in _SEPARATORS.split(identifier.upper()) if word]
    while words and words[0] in _VENDOR_PREFIXES:
        words.pop(0)
    while words and words[-1] in _TRAILING_NOISE:
        words.pop()
    return words


# Bus/form-factor words that trail a rebranded SKU string. Dropped only
# as a *lookup* fallback, never from a table key: `"H100 PCIe"` is a real
# table key and must keep matching that exact spelling, while HPE's
# `"H100 80GB PCIe"` has to fall back to the table's `"H100 80GB"`.
_FORM_FACTOR_WORDS = frozenset({"PCIE", "SXM", "SXM2", "SXM4", "SXM5", "OAM"})

# A trailing capacity word, e.g. the `48GB` of `L40S 48GB PCIe`. HPE
# names a card by its model *and* its capacity where the table is keyed
# on the model alone; matching the two is only safe when the capacity
# agrees, which `_for_identifier` checks against the row's own VRAM
# rather than assuming.
_CAPACITY_WORD = re.compile(r"(\d+)GB")


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
        pid (str): The identifier this definition is named for — a Cisco
            PID (`"UCSC-GPU-A100"`) for a Cisco-sourced row, or the
            canonical model string for a row no Cisco PID covers.
        name (str): The friendly name to report in place of the raw
            identifier, e.g. `"NVIDIA A100 40GB"`.
        memory_bytes (int): The card's known VRAM, in bytes.
        keys (tuple[str, ...]): Every normalized identifier that matches
            this definition, `pid`'s own included. Precomputed at
            construction — `GpuCatalog.enrich` compares against these, so
            normalizing per lookup would repeat the same work on every
            GPU of every server in the fleet.
    """

    pid: str
    name: str
    memory_bytes: int
    keys: tuple[str, ...] = ()


def _definition(
    pid: str, name: str, memory_bytes: int, identifiers: tuple[str, ...]
) -> GpuModelDefinition:
    """
    Build a definition with its normalized match keys filled in.

    Args:
        pid (str): The identifier the definition is named for.
        name (str): The friendly name to report.
        memory_bytes (int): The card's VRAM, in bytes.
        identifiers (tuple[str, ...]): Every spelling that should match,
            unnormalized.

    Returns:
        GpuModelDefinition: The definition, with duplicate and empty keys
            dropped.
    """
    keys: list[str] = []
    for identifier in identifiers:
        key = _normalize(identifier)
        if key and key not in keys:
            keys.append(key)
    return GpuModelDefinition(pid=pid, name=name, memory_bytes=memory_bytes, keys=tuple(keys))


def _built_in_definitions() -> tuple[GpuModelDefinition, ...]:
    """
    Build the shipped default table.

    Returns:
        tuple[GpuModelDefinition, ...]: One definition per row of
            `gpu_models.DEFAULT_GPU_MODELS`, in table order.
    """
    return tuple(
        _definition(identifiers[0], name, vram_gb * 1024**3, identifiers)
        for name, vram_gb, identifiers in DEFAULT_GPU_MODELS
    )


@dataclass(frozen=True, slots=True)
class GpuCatalog:
    """
    The GPUs this deployment can enrich.

    The shipped default table with `INVENTORY_GPU_MODELS` merged over it.
    Immutable once built, same reasoning as `SiteCatalog`: it can be
    shared freely and cannot drift mid-run.
    """

    definitions: tuple[GpuModelDefinition, ...]

    @classmethod
    def from_spec(cls, spec: str) -> GpuCatalog:
        """
        Parse `INVENTORY_GPU_MODELS` and merge it over the built-in table.

        The format is `PID:Friendly Name:VRAM_GB`, comma-separated:

            P1001-200:NVIDIA A100 40GB:40,P1010-200:NVIDIA H100 80GB:80

        The first field is a Cisco PID or a vendor model string — both
        match, normalized the same way (`_normalize`).

        **Configured entries override, they do not replace.** An entry
        whose identifier normalizes onto a built-in row's key wins for
        that key; every other built-in row survives. An empty spec is not
        an empty catalog — it is the built-in table alone, which is the
        point of shipping one.

        Args:
            spec (str): The raw configured value. Empty means the
                built-in table unmodified, not an error.

        Returns:
            GpuCatalog: Configured entries first, then the built-in rows
                they did not override.

        Raises:
            GpuCatalogConfigurationError: On a malformed or duplicate
                entry.
        """
        configured = cls._parse(spec)
        overridden = {key for definition in configured for key in definition.keys}
        built_in: list[GpuModelDefinition] = []
        for definition in _built_in_definitions():
            kept = tuple(key for key in definition.keys if key not in overridden)
            if kept:
                built_in.append(
                    GpuModelDefinition(
                        pid=definition.pid,
                        name=definition.name,
                        memory_bytes=definition.memory_bytes,
                        keys=kept,
                    )
                )
        return cls(definitions=(*configured, *built_in))

    @staticmethod
    def _parse(spec: str) -> tuple[GpuModelDefinition, ...]:
        """
        Parse the configured entries alone, without the built-in table.

        Args:
            spec (str): The raw `INVENTORY_GPU_MODELS` value.

        Returns:
            tuple[GpuModelDefinition, ...]: The parsed entries, in
                configured order.

        Raises:
            GpuCatalogConfigurationError: On a malformed or duplicate
                entry.
        """
        text = spec.strip()
        if not text:
            return ()

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
            key = _normalize(pid)
            if not key:
                raise GpuCatalogConfigurationError(
                    f"INVENTORY_GPU_MODELS: PID {pid!r} is nothing but vendor words and "
                    "punctuation, so it could never match a GPU."
                )
            if key in seen:
                raise GpuCatalogConfigurationError(
                    f"INVENTORY_GPU_MODELS: PID {pid!r} is listed twice."
                )
            seen.add(key)
            definitions.append(_definition(pid, name, vram_gb * 1024**3, (pid,)))
        return tuple(definitions)

    def _for_identifier(self, identifier: str) -> GpuModelDefinition | None:
        """
        Look up one PID or model string.

        Tried in order: the normalized string itself; the same string
        with a trailing bus/form-factor word removed; and finally a
        `<model><N>GB` spelling against a row keyed on the bare model,
        which is accepted only when N GB equals that row's known VRAM.
        The three exist because a vendor's own product name is not the
        chip's model string — HPE reports
        `"HPE NVIDIA L40S 48GB PCIe Accelerator"` where a BMC reports
        `"NVIDIA L40S"`. See docs/hpe-collectors.md, "GPUs".

        Args:
            identifier (str): The PID or model as a provider reported it.

        Returns:
            GpuModelDefinition | None: The matching definition, or `None`.
                Configured entries come first, so one always wins over
                the built-in row it overrides.
        """
        words = _words(identifier)
        if not words:
            return None
        candidates = ["".join(words)]
        if words[-1] in _FORM_FACTOR_WORDS:
            words = words[:-1]
            candidates.append("".join(words))
        for candidate in candidates:
            for definition in self.definitions:
                if candidate in definition.keys:
                    return definition

        # Last resort, and the only inexact one: a `<model> <N>GB`
        # spelling against a row keyed on the bare model. Accepted only
        # when N GB is that row's own VRAM, so a mismatched capacity
        # (HPE's 64GB A16 card, which this table models as four 16GB
        # GPUs) correctly finds nothing instead of reporting a wrong
        # number.
        capacity = _CAPACITY_WORD.fullmatch(words[-1]) if words else None
        if capacity is None:
            return None
        base = "".join(words[:-1])
        if not base:
            return None
        wanted = int(capacity.group(1)) * 1024**3
        for definition in self.definitions:
            if base in definition.keys and definition.memory_bytes == wanted:
                return definition
        return None

    def enrich(self, gpu: Mapping[str, Any]) -> dict[str, Any]:
        """
        Fill in a GPU's memory from this catalog, when the API left it
        unknown and this catalog recognizes the card.

        Real data always wins: a GPU whose `memory_bytes` a collector
        already populated is returned unchanged — this catalog only fills
        a gap, it never overrides a vendor's own answer, matching the
        platform-wide "a provider's `None` means unread, not zero"
        contract (`app.domain.ports.provider.ProviderServer`).

        `model` carries a Cisco PID on the Cisco collectors and a vendor
        model string on the Redfish-sourced ones; both are matched, and
        both after normalization, so case, whitespace, separators and a
        leading vendor word do not have to agree.

        Args:
            gpu (Mapping[str, Any]): One entry from `ProviderServer.gpus`
                — keys mirror `app.domain.models.hardware.Gpu`.

        Returns:
            dict[str, Any]: `gpu` unchanged, or a copy with `model`
                replaced by the friendly name and `memory_bytes` filled
                in, when `memory_bytes` was `None` and `model` matched.
        """
        if gpu.get("memory_bytes") is not None:
            return dict(gpu)
        model = gpu.get("model")
        if not isinstance(model, str):
            return dict(gpu)
        definition = self._for_identifier(model)
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
