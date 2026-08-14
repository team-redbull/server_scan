"""`ServerInventoryProvider` implementation for a single Cisco UCS Manager
domain — the real-collector counterpart to
`app.infrastructure.providers.fake.provider.FakeProvider`, satisfying the
exact same `app.domain.ports.provider.ServerInventoryProvider` Protocol so
`app.application.services.ingest.IngestService` doesn't need to know or
care whether it's ingesting fake or real data.

One instance is scoped to one UCS Manager domain (one `Manager` document,
`type=UCS_MANAGER`) — `tools.run_collector` constructs one provider per
manager and runs each through its own `IngestService.ingest()` call, so a
failure or slow domain never blocks the others.

Every lookup here is a *domain-wide* `query_classid` joined client-side by
distinguished name, never a per-server `query_children`. That's a
correctness requirement before it's a performance one: `mgmtIf` and
`adaptorHostEthIf` are both **grandchildren** of a compute unit, not
children of one, so a `configResolveChildren` scoped to a blade's DN and
filtered to either class matches nothing at all. Confirmed against the
installed `ucsmsdk==0.9.27` MO metadata:

    adaptorHostEthIf  parents=['adaptorUnit']                       rn=host-eth-[id]
    adaptorUnit       parents=['computeBlade','computeRackUnit',...] rn=adaptor-[id]
    mgmtIf            parents=['adaptorHostEthIf','mgmtController']  rn=if-[id]
    mgmtController    parents=[...,'computeBlade','computeRackUnit'] rn=mgmt

so the real DNs are `sys/chassis-1/blade-1/adaptor-1/host-eth-1` and
`sys/chassis-1/blade-1/mgmt/if-1` — two levels down. `hierarchy=True` does
not widen the class filter's depth; it only asks the server to attach each
*matched* object's subtree, which `ucsmsdk.ucscoreutils.
extract_molist_from_method_response` then flattens with no class filter at
all (so a match would return foreign MO classes mixed into the list).
Cisco's own SDK agrees: its blade -> mgmtIf lookup in `ucsmsdk/utils/
ucskvmlaunch.py` uses `configScope`, not `configResolveChildren`, and
`ucsmsdk/utils/inventory.py` collects adapters with a domain-wide
`query_classid`.

Joining on a DN prefix is exact rather than heuristic: `ucsmo.py` builds
every MO's `dn` as `parent_dn + "/" + rn`, so a descendant's DN always
starts with its owning server's DN followed by a separator.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any

from app.domain.enums import ManagerType
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerCredentials
from app.domain.ports.provider import ProviderServer
from app.infrastructure.providers.ucs_manager.client import UcsManagerClient
from app.infrastructure.providers.ucs_manager.mapping import compute_unit_to_provider_server

# See `ComputeBladeConsts`/`ComputeRackUnitConsts` in the installed
# `ucsmsdk`: the full presence enum is `empty`, `equipped`,
# `equipped-deprecated`, `equipped-identity-unestablishable`,
# `equipped-not-primary`, `equipped-slave`, `equipped-unsupported`,
# `equipped-with-malformed-fru`, `inaccessible`, `mismatch`,
# `mismatch-identity-unestablishable`, `mismatch-slave`, `missing`,
# `missing-slave`, `unauthorized`, `unknown`. Every "equipped*" variant is
# a physically-present server; no non-equipped value shares the prefix.
_EQUIPPED_PREFIX = "equipped"

# ...except these two, which are the *secondary* half of a multi-node
# server (a B460's slave blade, for instance): physically present, but not
# an independently addressable server. UCS Manager reports the logical
# server under the primary's DN, so ingesting these too would double-count
# one machine as two.
_NON_PRIMARY_PRESENCE = frozenset({"equipped-slave", "equipped-not-primary"})

# `lsServer` carries both real service profiles and the templates they're
# derived from, distinguished only by `type` — there is no separate
# `lsServiceProfileTemplate` class in UCS Manager's model (confirmed:
# `ucscoreutils.find_class_id_in_mo_meta_ignore_case` returns `None` for
# that name, and `LsServer.prop_meta["type"]` restricts to exactly these
# three values). One query returns both; partitioning happens here.
_TEMPLATE_TYPES = frozenset({"initial-template", "updating-template"})


def _is_equipped(server_mo: Any) -> bool:
    raw = getattr(server_mo, "presence", None)
    if not raw:
        return False
    presence = str(raw)
    if presence in _NON_PRIMARY_PRESENCE:
        return False
    return presence.startswith(_EQUIPPED_PREFIX)


def _group_by_owning_server_dn(
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
    no matter how large the domain is.
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


class UcsManagerProvider:
    provider_type = ManagerType.UCS_MANAGER.value

    def __init__(
        self, *, manager: Manager, credentials: ManagerCredentials, timeout_seconds: float
    ) -> None:
        if not manager.endpoint:
            raise ValueError(f"Manager {manager.id!r} has no endpoint configured.")
        # Narrowed to `str` here (once, where the check actually lives) so
        # `_new_client` doesn't need to re-prove `endpoint` is non-`None`
        # to the type checker on every call.
        self._endpoint: str = manager.endpoint
        self._manager = manager
        self._timeout_seconds = timeout_seconds
        self._credentials = credentials

    def _new_client(self) -> UcsManagerClient:
        # A fresh `UcsManagerClient` (and so a fresh login session) per
        # call — see `UcsManagerClient`'s docstring on why sessions aren't
        # shared/reused across calls.
        return UcsManagerClient(
            endpoint=self._endpoint,
            username=self._credentials.username,
            password=self._credentials.password,
            timeout_seconds=self._timeout_seconds,
        )

    async def health_check(self) -> None:
        client = self._new_client()
        try:
            await client.login()
        finally:
            await client.logout()

    async def list_servers(self) -> AsyncIterator[ProviderServer]:
        """Must be iterated to exhaustion (or explicitly closed via
        `contextlib.aclosing`) — abandoning this generator part-way leaves
        the `finally` below to run at GC time, and UCS Manager enforces a
        per-user session cap. `IngestService.ingest` drains it fully.
        """
        client = self._new_client()
        # `login()` belongs *inside* the try: `ucssession._login` sets the
        # session cookie and only then calls `_update_version()` /
        # `_update_domain_name_and_ip()`, either of which can raise with
        # the session already established server-side. Logging in outside
        # the try would leak that session until UCS Manager times it out.
        try:
            await client.login()

            blades = await client.query_classid("computeBlade")
            rack_units = await client.query_classid("computeRackUnit")
            ls_servers = await client.query_classid("lsServer")
            mgmt_ifs = await client.query_classid("mgmtIf")
            adapter_host_eth_ifs = await client.query_classid("adaptorHostEthIf")

            profile_by_dn: dict[str, Any] = {}
            template_dn_by_name: dict[str, str] = {}
            for mo in ls_servers:
                if str(getattr(mo, "type", "") or "") in _TEMPLATE_TYPES:
                    name = getattr(mo, "name", None)
                    if name:
                        # Bare template names are only unique within one
                        # org, so this mapping is lossy across orgs by
                        # construction — `mapping.py` prefers the profile's
                        # own resolved `oper_src_templ_name` DN and only
                        # falls back to this lookup.
                        template_dn_by_name.setdefault(name, mo.dn)
                else:
                    profile_by_dn[mo.dn] = mo

            servers = [mo for mo in (*blades, *rack_units) if _is_equipped(mo)]
            server_dns = [mo.dn for mo in servers]
            mgmt_ifs_by_server = _group_by_owning_server_dn(mgmt_ifs, server_dns=server_dns)
            adapter_ifs_by_server = _group_by_owning_server_dn(
                adapter_host_eth_ifs, server_dns=server_dns
            )

            for server_mo in servers:
                mgmt_if = next(
                    (
                        m
                        for m in mgmt_ifs_by_server[server_mo.dn]
                        if getattr(m, "access", None) == "out-of-band"
                    ),
                    None,
                )
                yield compute_unit_to_provider_server(
                    server_mo,
                    manager_id=self._manager.id,
                    site_id=self._manager.site_id,
                    profile_by_dn=profile_by_dn,
                    template_dn_by_name=template_dn_by_name,
                    mgmt_if=mgmt_if,
                    adapter_ifs=adapter_ifs_by_server[server_mo.dn],
                )
        finally:
            await client.logout()
