"""Pure `ucsmsdk` managed-object -> `ProviderServer` mapping. No I/O here —
`provider.py` does every XML API call and hands this module plain
`ucsmsdk` MO objects (or `None`/empty collections when a lookup found
nothing) to convert.

Field-by-field confidence, tracked explicitly because this was built
without a live UCS Manager domain to test against (see the session's
UCS-collector design discussion) — every attribute name below was
confirmed against the *installed* `ucsmsdk==0.9.27` package's generated
MO source (`ucsmsdk/mometa/**/*.py`'s `prop_meta` dicts), not just
documentation, but a few field *semantics* (units, exact object nesting)
were not independently verifiable without a real domain:

CONFIRMED (attribute exists, meaning matches Cisco's docs/property help):
  computeBlade / computeRackUnit: serial, model, vendor, num_of_cpus,
  num_of_cores, num_of_threads, total_memory, available_memory, presence,
  oper_state, uuid, original_uuid, assigned_to_dn, dn, name, server_id,
  chassis_id/slot_id (blade only).
  lsServer (service profile *and* template — one class, distinguished by
  `type`): src_templ_name, oper_src_templ_name, type, name, dn.
  mgmtIf: access ("out-of-band" is the value to filter on), ext_ip, mac.
  adaptorHostEthIf: switch_id, mac, admin_state, oper_state, id/name.

ASSUMED, NOT INDEPENDENTLY VERIFIED — flagged inline where used:
  - `total_memory`/`available_memory`'s unit. UCS Manager's own GUI
    labels this column "Total Memory (MB)", which is the basis for the
    MB->bytes conversion below. This one genuinely cannot be settled from
    the SDK: `prop_meta["total_memory"]` is a bare `uint` with no unit
    annotation, doc string or range — the package is code-generated from
    the MIT schema and carries no unit metadata for any property. Verify
    against UCSPE or real hardware.
  - Per-CPU model string and storage-drive detail: no MO for either was
    confirmed, so `cpu_model` stays `None` and `storage_*` stay at their
    zero/empty defaults — a v1 scope cut, not an oversight. A follow-up
    pass against a real UCS Manager (or Cisco's UCS Platform Emulator)
    should fill in CPU/storage detail.
"""

from __future__ import annotations

from typing import Any

from app.domain.ports.provider import ProviderAttachment, ProviderServer

_BYTES_PER_MB = 1024 * 1024


def _profile_template_fields(
    profile: Any | None,
    *,
    template_dn_by_name: dict[str, str],
) -> tuple[str | None, str | None]:
    """`(name, external_id)` for `ProviderServer.profile_template_*`, or
    `(None, None)` if the server has no assigned service profile (a
    discovered-but-unassociated blade, for instance) or its profile isn't
    itself derived from a template (a one-off, non-template profile).
    """
    if profile is None:
        return None, None
    template_name = getattr(profile, "src_templ_name", None) or None
    if not template_name:
        return None, None
    # The template's own `dn` (its full distinguished name, including org
    # path — e.g. "org-root/ls-template-mytemplate") is a real, stable,
    # unique identifier, unlike the bare name alone (which is only unique
    # *within* one org — two orgs can each own a "worker-template").
    #
    # `oper_src_templ_name` is UCS Manager's own resolved absolute DN for
    # the source template (the `oper*` convention across `lsServer`'s
    # policy-name properties), so it is preferred: it is collision-proof
    # across orgs, where the by-name lookup is lossy by construction.
    # Falls back to the by-name lookup, then to the bare name, for a
    # profile whose template was since deleted or renamed.
    oper_dn = getattr(profile, "oper_src_templ_name", None) or None
    external_id = oper_dn or template_dn_by_name.get(template_name, template_name)
    return template_name, external_id


def _server_name(server_mo: Any, profile: Any | None) -> str:
    """The name an operator would use for this machine.

    The associated service profile's name comes first, because that is
    what a UCS server is actually called: `computeBlade.name` is an
    optional user label that is *empty in practice* — verified against
    UCSPE 4.2, where a blade with a service profile bound to it still
    reported `name=""`. Falling back to the DN alone (as this did
    originally) would name every server `sys/chassis-1/blade-3`, which is
    a location, not an identity, and carries none of the information the
    rest of the platform reads out of a hostname: the site token
    (`app.domain.value_objects.site`) and the installation-type
    convention the classification rules match on. A UCS-sourced fleet
    would have been permanently unsited and unclassified.

    The DN remains the last resort, and stays the `external_id`
    regardless — identity and display name are different jobs.
    """
    profile_name = getattr(profile, "name", None) if profile is not None else None
    return profile_name or getattr(server_mo, "name", None) or str(server_mo.dn)


def _bmc_address(mgmt_if: Any | None) -> str | None:
    if mgmt_if is None:
        return None
    ext_ip = getattr(mgmt_if, "ext_ip", None)
    if not ext_ip or ext_ip in ("0.0.0.0", "none"):  # noqa: S104 - unset-IP sentinels, not a bind
        return None
    # Matches the `ipmi://` form `app.domain.value_objects.bmc_address.
    # parse_bmc_address` already recognizes for Cisco (see the fake
    # generator's `_bmc_address`) — a UCS-managed CIMC's out-of-band
    # interface is reachable the same way a standalone one would be.
    return f"ipmi://{ext_ip}:623"


def _nic_macs(adapter_ifs: list[Any]) -> tuple[str, ...]:
    macs: list[str] = []
    for mo in adapter_ifs:
        mac = getattr(mo, "mac", None)
        if mac and mac.lower() not in ("not applicable", "derived"):
            macs.append(mac)
    return tuple(macs)


# UCS reports interface state in its own vocabulary; the platform's
# `ConnectivityAttachment.oper_state` is documented as UP | DOWN | UNKNOWN
# and `compute_connectivity_facts` counts those exact strings. Passing the
# raw UCS value straight through left every fabric path counted as neither
# up nor down — verified against UCSPE 4.2, where a server with four
# attachments reported `fabric_paths_up: 0, fabric_paths_down: 0` — which
# silently disabled the connectivity health signal for every UCS server.
#
# `admin-down` maps to DISABLED rather than DOWN on purpose: it means the
# port was administratively disabled (the normal state of an adapter port
# on a server with no service profile associated), not that a cable or
# link failed. `compute_connectivity_facts` counts neither, so an
# unassociated server does not masquerade as a connectivity fault.
_OPER_STATE_MAP = {
    "operable": "UP",
    "up": "UP",
    "link-up": "UP",
    "admin-down": "DISABLED",
    "disabled": "DISABLED",
    "inoperable": "DOWN",
    "down": "DOWN",
    "link-down": "DOWN",
    "failed": "DOWN",
    "sfp-not-present": "DOWN",
}

_ADMIN_STATE_MAP = {"enabled": "ENABLED", "disabled": "DISABLED"}


def _oper_state(mo: Any) -> str:
    return _OPER_STATE_MAP.get(str(getattr(mo, "oper_state", "") or "").lower(), "UNKNOWN")


def _admin_state(mo: Any) -> str:
    return _ADMIN_STATE_MAP.get(str(getattr(mo, "admin_state", "") or "").lower(), "UNKNOWN")


def _attachments(adapter_ifs: list[Any], *, provider_type: str) -> tuple[ProviderAttachment, ...]:
    attachments: list[ProviderAttachment] = []
    for mo in adapter_ifs:
        switch_id = getattr(mo, "switch_id", None)
        if not switch_id or switch_id.upper() == "NONE":
            continue
        attachments.append(
            ProviderAttachment(
                type="FABRIC_INTERCONNECT",
                # Which collector observed this attachment, not which
                # product owns the fabric — a UCS Central run reports
                # UCS_CENTRAL for hardware that is still fronted by a
                # domain's own fabric interconnects.
                provider=provider_type,
                fabric=switch_id,
                # Not populated: resolving the fabric interconnect's own
                # name/model/serial needs a `networkElement` lookup this
                # collector does not make.
                fabric_name=None,
                fabric_id=None,
                fabric_model=None,
                fabric_serial=None,
                server_interface=getattr(mo, "name", None) or getattr(mo, "id", None),
                server_port=None,
                # Physical ports (`adaptorExtEthIf`) carry `peer_dn` — the
                # fabric-side port this adapter port is cabled to. Logical
                # vNICs (`adaptorHostEthIf`) have no such peer.
                fabric_port=getattr(mo, "peer_dn", None) or None,
                admin_state=_admin_state(mo),
                oper_state=_oper_state(mo),
                speed_mbps=None,
            )
        )
    return tuple(attachments)


def compute_unit_to_provider_server(
    server_mo: Any,
    *,
    manager_id: str,
    profile_by_dn: dict[str, Any],
    template_dn_by_name: dict[str, str],
    mgmt_if: Any | None,
    adapter_ifs: list[Any],
    provider_type: str = "UCS_MANAGER",
) -> ProviderServer:
    """Convert one `computeBlade` or `computeRackUnit` MO — the two
    classes carry the same relevant property set (see module docstring),
    so one function handles both rather than duplicating the mapping.

    Also serves the UCS Central collector unchanged: `ucscsdk` exposes
    every attribute read here under the same name as `ucsmsdk` (see
    `app.infrastructure.providers.ucs_common`), so only `provider_type`
    differs between the two callers.
    """
    profile = profile_by_dn.get(getattr(server_mo, "assigned_to_dn", None) or "")
    template_name, template_external_id = _profile_template_fields(
        profile, template_dn_by_name=template_dn_by_name
    )

    total_memory_mb = _as_int(getattr(server_mo, "total_memory", None))

    return ProviderServer(
        external_id=server_mo.dn,
        vendor="cisco",
        name=_server_name(server_mo, profile),
        model=getattr(server_mo, "model", None) or None,
        serial=getattr(server_mo, "serial", None) or None,
        system_uuid=getattr(server_mo, "uuid", None) or None,
        nic_macs=_nic_macs(adapter_ifs),
        bmc_address_raw=_bmc_address(mgmt_if),
        bmc_mac=getattr(mgmt_if, "mac", None) if mgmt_if is not None else None,
        manager_id=manager_id,
        profile_template_name=template_name,
        profile_template_external_id=template_external_id,
        cpu_sockets=_as_int(getattr(server_mo, "num_of_cpus", None)),
        cpu_cores=_as_int(getattr(server_mo, "num_of_cores", None)),
        cpu_threads=_as_int(getattr(server_mo, "num_of_threads", None)),
        cpu_model=None,  # see module docstring's ASSUMED section
        memory_total_bytes=total_memory_mb * _BYTES_PER_MB,
        storage_total_bytes=0,  # see module docstring's ASSUMED section
        storage_drives=(),
        attachments=_attachments(adapter_ifs, provider_type=provider_type),
        tags=(),
    )


def _as_int(value: object) -> int:
    """UCS XML attributes are always strings on the wire; `ucsmsdk`
    leaves numeric-looking ones as `str` rather than coercing them, so
    every numeric field here goes through this rather than assuming an
    `int` was handed back. Missing/non-numeric -> 0, never a raised
    exception — a single unparseable count shouldn't fail the whole
    server.
    """
    if value is None:
        return 0
    try:
        return int(str(value))
    except ValueError:
        return 0
