"""Text normalization for sort/search-safe fields.

Every function here is total (never raises, never returns `None` for a
non-null input) because the fields they populate
(`name_normalized`/`serial_normalized`/`model_normalized`) must always be
present — see `app.domain.models.server.Server`'s docstring on why that
matters for keyset pagination.
"""

from __future__ import annotations


def normalize_text(value: str | None) -> str:
    """Lowercase and collapse internal whitespace. Used for the
    `*_normalized` sort/filter fields — deliberately simpler than
    `build_search_tokens` (no splitting into tokens), since these fields
    are compared/sorted as whole strings, not searched.
    """
    if not value:
        return ""
    return " ".join(value.split()).lower()
