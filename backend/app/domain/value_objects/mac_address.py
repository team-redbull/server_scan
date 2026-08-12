"""MAC address normalization.

Hardware managers report MAC addresses in whatever format the vendor's API
happens to use. Cisco UCS in particular reports the "dotted" form
(`aabb.ccdd.eeff`), not colon-separated — normalizing every input format to
one canonical lowercase-colon form is what lets identity correlation
(`vendor + serial`, `bmc_mac`, `nic_macs`) treat two differently-formatted
reports of the same physical address as the same value, and what lets a
MAC-keyed PXE boot script (bare 12-hex, no separators) join against the
same server record.

Deliberately out of scope for Phase 1: 20-byte InfiniBand GUIDs. A GUID is
rejected (returns `None`) rather than silently truncated or mis-parsed.
"""

from __future__ import annotations

import re

_SEPARATORS = re.compile(r"[.:\-\s]")
_HEX12 = re.compile(r"^[0-9a-f]{12}$")

_ALL_ZERO = "000000000000"
_BROADCAST = "ffffffffffff"


def normalize_mac(raw: str | None) -> str | None:
    """Normalize a MAC address to lowercase, colon-separated form.

    Accepts colon (`aa:bb:cc:dd:ee:ff`), dash (`AA-BB-CC-DD-EE-FF`), Cisco
    dotted (`aabb.ccdd.eeff`), bare hex (`aabbccddeeff`), and
    space-separated forms, case-insensitively. Returns `None` for anything
    that isn't exactly 12 hex characters once separators are stripped,
    including the reserved all-zero and broadcast addresses (never a real
    NIC's burned-in address, and far more likely to indicate missing data
    than a real MAC).
    """
    if not raw:
        return None

    stripped = _SEPARATORS.sub("", raw).lower()

    if not _HEX12.match(stripped):
        return None
    if stripped in (_ALL_ZERO, _BROADCAST):
        return None

    return ":".join(stripped[i : i + 2] for i in range(0, 12, 2))
