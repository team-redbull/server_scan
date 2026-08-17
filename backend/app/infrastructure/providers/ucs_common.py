"""Logic shared by the two Cisco SDKs — `ucsmsdk` (UCS Manager, one domain)
and `ucscsdk` (UCS Central, every registered domain).

See docs/cisco-collectors.md, "Shared object model and DN joins".
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

_EQUIPPED_PREFIX = "equipped"
_NON_PRIMARY_PRESENCE = frozenset({"equipped-slave", "equipped-not-primary"})

TEMPLATE_TYPES = frozenset({"initial-template", "updating-template"})

_NON_BMC_ACCESS = frozenset({"in-band", "internal", "virtual"})


def is_equipped(server_mo: Any) -> bool:
    """
    Report whether a compute MO is a physically-present, independently
    addressable server.

    See docs/cisco-collectors.md, "Shared object model and DN joins".

    Args:
        server_mo (Any): A `computeBlade` or `computeRackUnit` managed
            object from either SDK.

    Returns:
        bool: True if the server is equipped and is not the secondary half
            of a multi-node server.
    """
    raw = getattr(server_mo, "presence", None)
    if not raw:
        return False
    presence = str(raw)
    if presence in _NON_PRIMARY_PRESENCE:
        return False
    return presence.startswith(_EQUIPPED_PREFIX)


def group_by_owning_server_dn(
    mos: Iterable[Any], *, server_dns: Iterable[str]
) -> dict[str, list[Any]]:
    """
    Bucket descendant managed objects under the compute unit each one lives
    below, dropping anything owned by something other than a server.

    See docs/cisco-collectors.md, "Shared object model and DN joins".

    Args:
        mos (Iterable[Any]): Descendant managed objects, typically the whole
            result of one domain-wide class query.
        server_dns (Iterable[str]): The compute-unit DNs to group under.

    Returns:
        dict[str, list[Any]]: One entry per DN in `server_dns`, each holding
            the MOs beneath it. DNs with no descendants map to an empty list.
    """
    known = set(server_dns)
    grouped: dict[str, list[Any]] = {dn: [] for dn in server_dns}
    for mo in mos:
        dn = getattr(mo, "dn", None)
        if not dn:
            continue
        ancestor, _, _ = str(dn).rpartition("/")
        while ancestor:
            if ancestor in known:
                grouped[ancestor].append(mo)
                break
            ancestor, _, _ = ancestor.rpartition("/")
    return grouped


def bmc_interface(mgmt_ifs: list[Any], *, server_dn: str) -> Any | None:
    """
    Pick a server's own CIMC management interface out of every `mgmtIf`
    under its DN.

    See docs/cisco-collectors.md, "BMC and management interface selection".

    Args:
        mgmt_ifs (list[Any]): Every `mgmtIf` found beneath `server_dn`,
            typically one bucket from `group_by_owning_server_dn`.
        server_dn (str): The owning compute unit's distinguished name.

    Returns:
        Any | None: The CIMC interface, or None if the server exposes none.
    """
    own_controller_prefix = f"{server_dn}/mgmt/"
    own = [
        mo
        for mo in mgmt_ifs
        if str(getattr(mo, "dn", "")).startswith(own_controller_prefix)
        and str(getattr(mo, "access", "") or "").lower() not in _NON_BMC_ACCESS
    ]
    if not own:
        return None
    return next(
        (mo for mo in own if getattr(mo, "access", None) == "out-of-band"),
        own[0],
    )


def partition_profiles(ls_servers: Iterable[Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """
    Split one `lsServer` query into real service profiles and the templates
    they derive from.

    See docs/cisco-collectors.md, "Service profiles and server names".

    Args:
        ls_servers (Iterable[Any]): The full result of one `lsServer` query,
            carrying both profiles and templates.

    Returns:
        tuple[dict[str, Any], dict[str, str]]: Service profiles keyed by DN,
            and template DNs keyed by bare template name.
    """
    profile_by_dn: dict[str, Any] = {}
    template_dn_by_name: dict[str, str] = {}
    for mo in ls_servers:
        if str(getattr(mo, "type", "") or "") in TEMPLATE_TYPES:
            name = getattr(mo, "name", None)
            if name:
                template_dn_by_name.setdefault(name, mo.dn)
        else:
            profile_by_dn[mo.dn] = mo
    return profile_by_dn, template_dn_by_name
