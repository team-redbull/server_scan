"""`app.domain.value_objects.gpu_catalog` — the deployment-supplied
PID-to-VRAM lookup, filling a gap neither Cisco management plane this
platform collects from (Intersight, UCS Manager) closes on its own.

Two things matter more than the happy path: a malformed
`INVENTORY_GPU_MODELS` entry must fail loudly at startup (the same
"config, not code" contract `INVENTORY_SITES` already has — see
docs/adr/0018-sites-from-configuration.md), and `enrich()` must never
override a value a collector actually reported, only fill in what it
left `None`.
"""

from __future__ import annotations

import pytest

from app.domain.value_objects.gpu_catalog import (
    GpuCatalog,
    GpuCatalogConfigurationError,
    gpu_catalog,
)

pytestmark = pytest.mark.unit

_SPEC = "P1001-200:NVIDIA A100 40GB:40,P1010-200:NVIDIA H100 80GB:80"


class TestFromSpec:
    def test_empty_spec_is_an_empty_catalog_not_an_error(self) -> None:
        """Unlike `INVENTORY_SITES`, there is no shipped default — an
        unconfigured deployment enriches nothing, which is correct.
        """
        catalog = GpuCatalog.from_spec("")
        assert catalog.definitions == ()

    def test_parses_pid_name_and_vram(self) -> None:
        catalog = GpuCatalog.from_spec(_SPEC)
        assert len(catalog.definitions) == 2
        a100 = next(d for d in catalog.definitions if d.pid == "P1001-200")
        assert a100.name == "NVIDIA A100 40GB"
        assert a100.memory_bytes == 40 * 1024**3

    def test_whitespace_around_entries_and_fields_is_trimmed(self) -> None:
        catalog = GpuCatalog.from_spec(" P1001-200 : NVIDIA A100 40GB : 40 ")
        assert catalog.definitions[0].pid == "P1001-200"
        assert catalog.definitions[0].name == "NVIDIA A100 40GB"

    def test_blank_entries_between_commas_are_skipped(self) -> None:
        catalog = GpuCatalog.from_spec("P1001-200:NVIDIA A100 40GB:40,,")
        assert len(catalog.definitions) == 1

    @pytest.mark.parametrize(
        "entry",
        [
            "P1001-200",
            "P1001-200:NVIDIA A100 40GB",
            "P1001-200:NVIDIA A100 40GB:40:extra",
        ],
    )
    def test_wrong_number_of_colon_parts_is_rejected(self, entry: str) -> None:
        with pytest.raises(GpuCatalogConfigurationError, match="PID:Friendly Name:VRAM_GB"):
            GpuCatalog.from_spec(entry)

    def test_empty_pid_is_rejected(self) -> None:
        with pytest.raises(GpuCatalogConfigurationError, match="names no PID"):
            GpuCatalog.from_spec(":NVIDIA A100 40GB:40")

    def test_empty_name_is_rejected(self) -> None:
        """Unlike a site code, there is no way to derive a friendly GPU
        name from a bare PID, so this cannot fall back the way
        `INVENTORY_SITES`' display-name half does.
        """
        with pytest.raises(GpuCatalogConfigurationError, match="names no friendly name"):
            GpuCatalog.from_spec("P1001-200::40")

    @pytest.mark.parametrize("vram", ["forty", "40.5", "0", "-40", ""])
    def test_non_positive_whole_vram_is_rejected(self, vram: str) -> None:
        with pytest.raises(GpuCatalogConfigurationError):
            GpuCatalog.from_spec(f"P1001-200:NVIDIA A100 40GB:{vram}")

    def test_duplicate_pid_is_rejected(self) -> None:
        with pytest.raises(GpuCatalogConfigurationError, match="listed twice"):
            GpuCatalog.from_spec("P1001-200:NVIDIA A100 40GB:40,P1001-200:NVIDIA A100 40GB:40")

    def test_duplicate_pid_check_is_case_insensitive(self) -> None:
        """The same PID typed in a different case is still the same PID —
        `enrich()` matches case-insensitively, so the catalog must reject
        what would otherwise be an ambiguous duplicate at lookup time.
        """
        with pytest.raises(GpuCatalogConfigurationError, match="listed twice"):
            GpuCatalog.from_spec("p1001-200:A:40,P1001-200:B:40")


class TestGpuCatalogCache:
    def test_gpu_catalog_is_cached_per_spec(self) -> None:
        assert gpu_catalog(_SPEC) is gpu_catalog(_SPEC)


class TestEnrich:
    def test_fills_in_memory_and_friendly_name_on_a_known_pid(self) -> None:
        catalog = GpuCatalog.from_spec(_SPEC)
        gpu = {"model": "P1001-200", "vendor": "NVIDIA", "memory_bytes": None}

        enriched = catalog.enrich(gpu)

        assert enriched["model"] == "NVIDIA A100 40GB"
        assert enriched["memory_bytes"] == 40 * 1024**3

    def test_match_is_case_insensitive(self) -> None:
        catalog = GpuCatalog.from_spec(_SPEC)
        gpu = {"model": "p1001-200", "memory_bytes": None}

        assert catalog.enrich(gpu)["memory_bytes"] == 40 * 1024**3

    def test_unknown_pid_is_returned_unchanged(self) -> None:
        catalog = GpuCatalog.from_spec(_SPEC)
        gpu = {"model": "P9999-999", "memory_bytes": None}

        enriched = catalog.enrich(gpu)

        assert enriched == gpu
        assert enriched is not gpu  # still a copy, not the same object

    def test_a_real_reported_memory_value_is_never_overridden(self) -> None:
        """A provider's `None` means unread, not zero — the same contract
        `ProviderServer` uses everywhere else. A GPU whose memory some
        future API version actually reports must win over this catalog,
        never the other way around.
        """
        catalog = GpuCatalog.from_spec(_SPEC)
        gpu = {"model": "P1001-200", "memory_bytes": 12345}

        enriched = catalog.enrich(gpu)

        assert enriched["memory_bytes"] == 12345
        assert enriched["model"] == "P1001-200"

    def test_empty_catalog_enriches_nothing(self) -> None:
        catalog = GpuCatalog.from_spec("")
        gpu = {"model": "P1001-200", "memory_bytes": None}

        assert catalog.enrich(gpu) == gpu

    def test_missing_or_non_string_model_is_handled_without_raising(self) -> None:
        catalog = GpuCatalog.from_spec(_SPEC)
        assert catalog.enrich({"memory_bytes": None}) == {"memory_bytes": None}
        assert catalog.enrich({"model": None, "memory_bytes": None}) == {
            "model": None,
            "memory_bytes": None,
        }
