"""Logic shared by the two Cisco collectors — UCS Manager (one domain,
`ucsmsdk`) and UCS Central (every registered domain, `ucscsdk`).

This module exists because the two SDKs describe the *same object model*.
Verified property-by-property against both installed packages: every
attribute `mapping.py` reads — `presence`, `assigned_to_dn`, `total_memory`,
`num_of_cpus`, `mgmtIf.ext_ip`/`access`, `adaptorExtEthIf.switch_id`/
`peer_dn`, `lsServer.type`/`oper_src_templ_name` — exists under the same
name in `ucscsdk.mometa.*` as in `ucsmsdk.mometa.*`, and the `presence`
enum is the same set minus `equipped-deprecated`.

What differs is only the DN *root*: `ucsmsdk` trees hang off `sys`
(`sys/chassis-1/blade-1`), `ucscsdk` trees off a per-domain
`compute/sys-<domainId>` (`compute/sys-1009/chassis-1/blade-1`). Every
function here works on relative structure — DN ancestry, prefix
containment — never on an absolute root, so both roots work unchanged.

Living here rather than duplicated per provider is deliberate: the
grouping and BMC-selection rules below were each wrong in a way that only
a live UCS Platform Emulator exposed (`docs/adr/0009`), and two copies
means the next such fix lands in one of them.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# See `ComputeBladeConsts`/`ComputeRackUnitConsts` in either SDK: the full
# presence enum is `empty`, `equipped`, `equipped-deprecated` (ucsmsdk
# only), `equipped-identity-unestablishable`, `equipped-not-primary`,
# `equipped-slave`, `equipped-unsupported`, `equipped-with-malformed-fru`,
# `inaccessible`, `mismatch`, `mismatch-identity-unestablishable`,
# `mismatch-slave`, `missing`, `missing-slave`, `unauthorized`, `unknown`.
# Every "equipped*" variant is a physically-present server; no
# non-equipped value shares the prefix.
_EQUIPPED_PREFIX = "equipped"

# ...except these two, which are the *secondary* half of a multi-node
# server (a B460's slave blade, for instance): physically present, but not
# an independently addressable server. Both SDKs report the logical server
# under the primary's DN, so ingesting these too would double-count one
# machine as two.
_NON_PRIMARY_PRESENCE = frozenset({"equipped-slave", "equipped-not-primary"})

# `lsServer` carries both real service profiles and the templates they are
# derived from, distinguished only by `type` — there is no separate
# `lsServiceProfileTemplate` class in either model (confirmed: both SDKs'
# `find_class_id_in_mo_meta_ignore_case` return `None` for that name, and
# `LsServer.prop_meta["type"]` restricts to exactly these three values in
# both). One query returns both; partitioning happens in the provider.
TEMPLATE_TYPES = frozenset({"initial-template", "updating-template"})

# `MgmtIfConsts.ACCESS_*` values that are never a BMC address, whatever
# their position in the tree. Everything else (`out-of-band`, and the
# `unspecified` a real blade reports) is accepted.
_NON_BMC_ACCESS = frozenset({"in-band", "internal", "virtual"})


def is_equipped(server_mo: Any) -> bool:
    """True for a physically-present, independently-addressable server."""
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
    """Bucket descendant MOs under the compute-unit DN each one lives
    below. A domain-wide `query_classid` also returns instances owned by
    chassis, fabric interconnects and IO modules (`mgmtIf` in particular
    hangs off a dozen different parent classes), so anything that isn't
    under one of `server_dns` is dropped rather than mis-attributed.

    Walks each MO's own ancestor DNs (nearest first) rather than scanning
    every server's DN as a prefix: it makes the match exact on segment
    boundaries for free (so `sys/rack-unit-1` can't claim
    `sys/rack-unit-10`'s descendants), gives nearest-ancestor-wins for
    free (so a nested `computeServerUnit` keeps its own descendants
    instead of donating them to its enclosing server), and is O(MOs x DN
    depth) instead of O(MOs x servers). DN depth is a handful of segments
    no matter how large the domain — or how many domains — is.

    Root-agnostic, which is what lets UCS Central reuse it: it only ever
    compares a DN against the server DNs it was given, so a
    `compute/sys-1009/...` tree groups exactly as a `sys/...` one does.
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
    """The server's own CIMC management interface, out of every `mgmtIf`
    that lives somewhere under its DN.

    Selected by position in the tree, not by `access`. An earlier version
    filtered on `access == "out-of-band"`, which turns out to select
    nothing on a real domain: verified against UCSPE 4.2, a blade's own
    management interfaces report `access="unspecified"` (with
    `subject="blade"`), and the only two `out-of-band` interfaces in the
    whole domain belong to the fabric interconnects themselves
    (`subject="switch"`), which are not under any server's DN at all.

    A compute unit owns exactly one management controller at
    `{server_dn}/mgmt`; the interfaces beneath it are the CIMC's. The
    other `mgmtIf`s under a server hang off its adapters
    (`{server_dn}/adaptor-N/mgmt/...`, `access="internal"`) and are not
    the BMC. `access == "out-of-band"` is still preferred when present,
    for domains that do set it.
    """
    own_controller_prefix = f"{server_dn}/mgmt/"
    own = [
        mo
        for mo in mgmt_ifs
        if str(getattr(mo, "dn", "")).startswith(own_controller_prefix)
        # `in-band` management rides the data path rather than the CIMC,
        # so its address is not the BMC address even though it hangs off
        # the same controller. `internal` is adapter-internal plumbing.
        and str(getattr(mo, "access", "") or "").lower() not in _NON_BMC_ACCESS
    ]
    if not own:
        return None
    return next(
        (mo for mo in own if getattr(mo, "access", None) == "out-of-band"),
        own[0],
    )


def partition_profiles(ls_servers: Iterable[Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Split one `lsServer` query into `(profile_by_dn, template_dn_by_name)`.

    Bare template names are only unique within one org, so the second
    mapping is lossy across orgs by construction — `mapping.py` prefers
    the profile's own resolved `oper_src_templ_name` DN and only falls
    back to this lookup.
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
