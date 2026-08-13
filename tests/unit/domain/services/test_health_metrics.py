import pytest

from app.domain.services.health.metrics import (
    MetricDef,
    MetricRegistry,
    MetricType,
    build_default_registry,
)


def test_register_and_get() -> None:
    r = MetricRegistry()
    r.register(
        MetricDef(
            name="x", type=MetricType.INT, category="cpu", description="", resolver=lambda f: 1
        )
    )
    assert r.get("x") is not None
    assert "x" in r


def test_duplicate_registration_raises() -> None:
    r = MetricRegistry()
    r.register(
        MetricDef(
            name="x", type=MetricType.INT, category="cpu", description="", resolver=lambda f: 1
        )
    )
    with pytest.raises(ValueError, match="already registered"):
        r.register(
            MetricDef(
                name="x", type=MetricType.INT, category="cpu", description="", resolver=lambda f: 1
            )
        )


def test_get_unknown_returns_none() -> None:
    r = MetricRegistry()
    assert r.get("nonexistent") is None


def test_default_registry_has_core_metrics() -> None:
    registry = build_default_registry()
    assert registry.get("connectivity.fabric_paths_down") is not None
    assert registry.get("storage.failed_drive_count") is not None
    names = {m.name for m in registry.all()}
    assert len(names) >= 10
