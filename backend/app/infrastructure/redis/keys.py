"""Redis cache key builders.

`si:<version>:...` namespaces every key so a future incompatible cache
payload change (e.g. adding a field the old format didn't have) can bump
`_NAMESPACE_VERSION` and get a clean cache rather than serving stale/
malformed payloads to a newer app version.

`server_key` embeds the document's `revision` rather than requiring an
explicit cache-invalidation call on update: a write bumps `revision`
(`AuditFields`), which changes the key, so the old entry is simply never
read again and expires on its own TTL — no invalidation path to get wrong
or forget.
"""

from __future__ import annotations

_NAMESPACE_VERSION = 1


def server_key(server_id: str, revision: int) -> str:
    return f"si:{_NAMESPACE_VERSION}:srv:{server_id}:r{revision}"


def list_key(filter_hash: str, cursor_hash: str) -> str:
    return f"si:{_NAMESPACE_VERSION}:list:{filter_hash}:{cursor_hash}"
