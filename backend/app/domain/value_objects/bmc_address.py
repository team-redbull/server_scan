"""BMC address parsing.

Hardware managers report BMC/iDRAC/iLO endpoints as vendor-specific URL
forms, not plain hostnames:

    idrac-virtualmedia://10.1.1.5/redfish/v1/Systems/System.Embedded.1   (Dell)
    redfish-virtualmedia://10.1.1.6/redfish/v1/Systems/1                (HPE)
    ipmi://10.1.1.7:623                                                 (Cisco, explicit port)
    ipmi://10.1.1.7                                                     (Cisco, implicit port 623)

Both the raw string and the parsed components are kept on the server
document: the raw form round-trips cleanly into a Metal3 `BareMetalHost`
CR's `spec.bmc.address` later, while the parsed `host` is what identity
correlation and search actually key on — two records reporting
`idrac-virtualmedia://10.1.1.5/...` and `ipmi://10.1.1.5:623` should
correlate as "the same host", which byte-equality on the raw string alone
would miss.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

# Schemes whose absent port has a well-known default. Only `ipmi` has one
# in practice (623 is IPMI's standard port); Redfish-over-HTTPS schemes are
# left with `port=None` when unspecified rather than guessing 443, since
# vendor Redfish endpoints are frequently proxied on non-standard ports.
_DEFAULT_PORTS: dict[str, int] = {"ipmi": 623}


@dataclass(frozen=True, slots=True)
class BmcAddress:
    raw: str
    scheme: str | None
    host: str | None
    host_is_ip: bool
    port: int | None
    path: str | None


def parse_bmc_address(raw: str | None) -> BmcAddress | None:
    """Parse a BMC address into its components. Returns `None` for empty
    input; never raises on malformed input — worst case, `host` is `None`
    and the raw string is preserved for a human to look at.
    """
    if not raw or not raw.strip():
        return None

    text = raw.strip()

    # A bare host/IP with no "scheme://" prefix is valid input (some
    # providers report just an address) but `urlsplit` would otherwise
    # parse the whole thing as a relative path with no netloc.
    candidate = text if "://" in text else f"//{text}"

    parts = urlsplit(candidate)
    scheme = parts.scheme or None
    host = parts.hostname  # urlsplit lowercases and strips IPv6 brackets
    port = parts.port
    path = parts.path or None

    if port is None and scheme in _DEFAULT_PORTS:
        port = _DEFAULT_PORTS[scheme]

    host_is_ip = False
    if host is not None:
        try:
            ipaddress.ip_address(host)
            host_is_ip = True
        except ValueError:
            host_is_ip = False

    return BmcAddress(
        raw=text,
        scheme=scheme,
        host=host,
        host_is_ip=host_is_ip,
        port=port,
        path=path,
    )
