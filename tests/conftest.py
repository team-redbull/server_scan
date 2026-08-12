from __future__ import annotations

import os

# Point the app at the CI/dev-up Mongo+Redis before any app module (which
# reads Settings at import/dependency time) is imported.
os.environ.setdefault("INVENTORY_MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("INVENTORY_REDIS_URI", "redis://localhost:6379/0")
os.environ.setdefault("INVENTORY_ENVIRONMENT", "test")
os.environ.setdefault("INVENTORY_MONGO_DB", "server_inventory_test")

# asyncio_mode = "auto" in pyproject.toml means pytest-asyncio picks up
# every `async def test_...` automatically — no per-test marker needed.
