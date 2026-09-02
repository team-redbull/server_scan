"""`app.config.settings.Settings` — pinning the one thing that goes wrong
silently: a field name that doesn't match its documented `INVENTORY_`
env var.

`Settings` derives each env var name from its field name
(`env_prefix="INVENTORY_"`, no per-field alias anywhere in this class),
and `extra="ignore"` means an unrecognized env var never raises — so a
field named `gpu_model_catalog` silently reads `INVENTORY_GPU_MODEL_CATALOG`
while every doc (`.env.example`, the Helm chart, this module's own
docstring) told an operator to set `INVENTORY_GPU_MODELS`, and nothing
ever complained. Confirmed live before the fix: `Settings()` returned the
`_CATALOG`-suffixed value and silently dropped `INVENTORY_GPU_MODELS`.
These tests exist so that specific class of bug can't come back unnoticed
for any field this pins.
"""

from __future__ import annotations

import pytest

from app.config.settings import Settings

pytestmark = pytest.mark.unit


def _settings() -> Settings:
    """Build `Settings` from the current process env alone, ignoring any
    real `.env` file on disk — a developer's local `.env` must not make
    this test's outcome depend on what happens to be sitting in their
    checkout. Callers set the env they care about via `monkeypatch`
    before calling this.

    Returns:
        Settings: Constructed from the process env, `.env` excluded.
    """
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestGpuModels:
    def test_inventory_gpu_models_populates_gpu_models(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact regression this module exists to catch."""
        monkeypatch.setenv(
            "INVENTORY_GPU_MODELS", "P1001-200:NVIDIA A100 40GB:40,P1001-220:NVIDIA A100 80GB:80"
        )
        settings = _settings()
        assert settings.gpu_models == "P1001-200:NVIDIA A100 40GB:40,P1001-220:NVIDIA A100 80GB:80"

    def test_unset_gpu_models_is_the_documented_empty_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("INVENTORY_GPU_MODELS", raising=False)
        assert _settings().gpu_models == ""


class TestSitesStillMatchesTheSameConvention:
    def test_inventory_sites_populates_sites(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not a new regression — confirms `gpu_models` was brought in
        line with the convention this field already followed correctly,
        rather than the other way around.
        """
        monkeypatch.setenv("INVENTORY_SITES", "tlv:Tel Aviv")
        assert _settings().sites == "tlv:Tel Aviv"
