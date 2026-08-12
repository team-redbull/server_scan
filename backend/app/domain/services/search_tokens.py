"""Builds the `search_tokens` field that backs safe user search.

Design (see `docs/adr/` and the session's approved plan): normal user
search is an escaped, anchored-prefix match against this multikey-indexed
field — never raw regex against arbitrary document fields, and never an
unanchored/unescaped pattern that could turn into a collection scan or a
ReDoS vector. This module only builds the token set; the query itself is
built in `app.domain.services.search`.

Token sources: name, hostname-ish identity fields, serial, model, vendor,
site/manager references, tags, and both the colon-form and bare-hex form
of every MAC (so `aa:bb:cc:dd:ee:ff` and `aabbccddeeff` both find the same
server — bare-hex is how the existing `map-pxe` boot scripts key on MACs).
"""

from __future__ import annotations

import re

from app.domain.models.server import Server

_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_MAX_TOKENS = 64
_MIN_TOKEN_LEN = 2
_MAX_TOKEN_LEN = 64


def _add(tokens: set[str], value: str | None) -> None:
    if not value:
        return
    lowered = value.lower()
    if len(lowered) >= _MIN_TOKEN_LEN:
        tokens.add(lowered[:_MAX_TOKEN_LEN])
    for part in _SPLIT_RE.split(lowered):
        if len(part) >= _MIN_TOKEN_LEN:
            tokens.add(part[:_MAX_TOKEN_LEN])


def _add_mac(tokens: set[str], mac: str | None) -> None:
    """Adds both the canonical colon form and the bare-hex form of a
    (already-normalized) MAC address as searchable tokens.
    """
    if not mac:
        return
    tokens.add(mac)
    tokens.add(mac.replace(":", ""))


def build_search_tokens(server: Server) -> list[str]:
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
