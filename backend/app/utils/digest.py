"""Stable hashing for cache keys.

Used to turn a filter/sort/pagination combination into a short, deterministic
cache-key component. Must be stable across processes and Python versions —
`hash()` is not (it is salted per-process for security), which is why this
goes through `hashlib` over a canonical JSON encoding instead.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def stable_hash(value: Any) -> str:
    """Return a short, deterministic hex digest of `value`.

    `value` must be JSON-serializable. Dict key order does not affect the
    result: `json.dumps(..., sort_keys=True)` canonicalizes it first.
    """
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
