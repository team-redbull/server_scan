"""Builds the `search_tokens` field that backs safe user search.

Design (see `docs/adr/` and the session's approved plan): normal user
search is an escaped, anchored-prefix match against this multikey-indexed
field — never raw regex against arbitrary document fields, and never an
unanchored/unescaped pattern that could turn into a collection scan or a
ReDoS vector. This module only builds the token set; the query itself is
built in `app.domain.services.search`.

Every token is a suffix of its source value starting at a word boundary,
so an anchored query finds a fragment from the middle of a name — why,
and what it costs, in `docs/adr/0025-search-tokens-are-word-boundary-suffixes.md`.

Token sources: name, hostname-ish identity fields, serial, model, vendor,
site/manager references, tags, the BMC's own address
(`network.bmc.host` — the same value `NetworkTab.tsx` shows as
"Address", so what an operator copies off a server's own page is what
finds it), and both the colon-form and bare-hex form of every MAC (so
`aa:bb:cc:dd:ee:ff` and `aabbccddeeff` both find the same server —
bare-hex is how the existing `map-pxe` boot scripts key on MACs).
"""

from __future__ import annotations

import re

from app.domain.models.server import Server

_PART_RE = re.compile(r"[a-z0-9]+")
_MAX_TOKENS = 64
_MIN_TOKEN_LEN = 2
_MAX_TOKEN_LEN = 64


def _add(tokens: set[str], value: str | None) -> None:
    """Index every word-boundary suffix of one value.

    Args:
        tokens (set[str]): The token set being built, mutated in place.
        value (str | None): The source value, or None to skip.
    """
    if not value:
        return
    lowered = value.lower()
    for part in _PART_RE.finditer(lowered):
        suffix = lowered[part.start() :]
        if len(suffix) >= _MIN_TOKEN_LEN:
            tokens.add(suffix[:_MAX_TOKEN_LEN])


def _add_mac(tokens: set[str], mac: str | None) -> None:
    """Add both the colon form and bare-hex form of an already-normalized MAC address."""
    if not mac:
        return
    tokens.add(mac)
    tokens.add(mac.replace(":", ""))


def build_search_tokens(server: Server) -> list[str]:
    """
    Build the multikey-indexed `search_tokens` field for one server.

    Args:
        server (Server): The server to tokenize.

    Returns:
        list[str]: The sorted, deduplicated token set, capped at `_MAX_TOKENS`.
    """
    tokens: set[str] = set()

    _add(tokens, server.name)
    _add(tokens, server.model)
    _add(tokens, server.identity.serial)
    _add(tokens, server.identity.system_uuid)
    _add(tokens, server.identity.vendor.value)
    _add(tokens, server.site_id)
    _add(tokens, server.manager_id)
    _add(tokens, server.classification.installation_type.value)

    for tag in server.tags:
        _add(tokens, tag)

    _add_mac(tokens, server.network.bmc.mac)
    for mac in server.identity.nic_macs[:8]:  # bounded: avoid token blowup
        _add_mac(tokens, mac)

    if server.network.bmc.host:
        _add(tokens, server.network.bmc.host)

    # Deterministic output (stable for golden-fixture tests), capped so a
    # pathological document (many tags, many NICs) can't blow up index
    # size unboundedly.
    return sorted(tokens)[:_MAX_TOKENS]
