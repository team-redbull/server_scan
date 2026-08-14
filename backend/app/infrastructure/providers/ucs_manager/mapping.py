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
  lsServer (service profile): src_templ_name, dn.
  mgmtIf: access ("out-of-band" is the value to filter on), ext_ip, mac.
  adaptorHostEthIf: switch_id, mac, admin_state, oper_state, id/name.

ASSUMED, NOT INDEPENDENTLY VERIFIED — flagged inline where used:
  - `total_memory`/`available_memory`'s unit. UCS Manager's own GUI
    labels this column "Total Memory (MB)", which is the basis for the
    MB->bytes conversion below; the XML attribute's own doc string
    wasn't fetched to confirm the unit matches the GUI label exactly.
  - `mgmtIf`'s exact position in the MO tree relative to a compute unit
    (queried via a hierarchical `query_children`, which is robust to not
    knowing the exact depth, at the cost of a slightly wider scan).
  - Per-CPU model string and storage-drive detail: no MO for either was
    confirmed, so `cpu_model` stays `None` and `storage_*` stay at their
    zero/empty defaults — a v1 scope cut, not an oversight. A follow-up
    pass against a real UCS Manager (or Cisco's UCS Platform Emulator)
    should verify the memory unit and fill in CPU/storage detail.
"""

from __future__ import annotations

from typing import Any

from app.domain.ports.provider import ProviderAttachment, ProviderServer

_BYTES_PER_MB = 1024 * 1024


def _profile_template_fields(
    server_mo: Any,
    *,
    profile_by_dn: dict[str, Any],
    template_dn_by_name: dict[str, str],
) -> tuple[str | None, str | None]:
    """`(name, external_id)` for `ProviderServer.profile_template_*`, or
    `(None, None)` if the server has no assigned service profile (a
    discovered-but-unassociated blade, for instance) or its profile isn't
    itself derived from a template (a one-off, non-template profile).
    """
    profile = profile_by_dn.get(server_mo.assigned_to_dn or "")
    if profile is None:
        return None, None
    template_name = profile.src_templ_name or None
    if not template_name:
        return None, None
    # The template's own `dn` (its full distinguished name, including org
    # path — e.g. "org-root/ls-template-mytemplate") is a real, stable,
    # unique identifier, unlike the bare name alone (which is only unique
    # within one org). Falls back to the name itself if the template
    # lookup didn't find a matching `lsServiceProfileTemplate` (e.g. a
    # profile cloned from a template that was since deleted).
    external_id = template_dn_by_name.get(template_name, template_name)
    return template_name, external_id


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


def _attachments(adapter_ifs: list[Any]) -> tuple[ProviderAttachment, ...]:
    attachments: list[ProviderAttachment] = []
    for mo in adapter_ifs:
        switch_id = getattr(mo, "switch_id", None)
        if not switch_id or switch_id.upper() == "NONE":
            continue
        attachments.append(
            ProviderAttachment(
                type="FABRIC_INTERCONNECT",
                provider="UCS_MANAGER",
                fabric=switch_id,
                # Not populated for v1 — see module docstring's ASSUMED
                # section: resolving the fabric interconnect's own
                # name/model/serial needs a `networkElement`/`fabricSwitch`
                # lookup this pass didn't confirm the shape of.
                fabric_name=None,
                fabric_id=None,
                fabric_model=None,
                fabric_serial=None,
                server_interface=getattr(mo, "name", None) or getattr(mo, "id", None),
                server_port=None,
                fabric_port=None,
                admin_state=getattr(mo, "admin_state", "") or "",
                oper_state=getattr(mo, "oper_state", "") or "",
                speed_mbps=None,
            )
        )
    return tuple(attachments)


def compute_unit_to_provider_server(
    server_mo: Any,
    *,
    manager_id: str,
    site_id: str | None,
    profile_by_dn: dict[str, Any],
    template_dn_by_name: dict[str, str],
    mgmt_if: Any | None,
    adapter_ifs: list[Any],
) -> ProviderServer:
    """Convert one `computeBlade` or `computeRackUnit` MO — the two
    classes carry the same relevant property set (see module docstring),
    so one function handles both rather than duplicating the mapping.
    """
    template_name, template_external_id = _profile_template_fields(
        server_mo, profile_by_dn=profile_by_dn, template_dn_by_name=template_dn_by_name
    )

    total_memory_mb = _as_int(getattr(server_mo, "total_memory", None))

    return ProviderServer(
        external_id=server_mo.dn,
        vendor="cisco",
        name=getattr(server_mo, "name", None) or server_mo.dn,
        model=getattr(server_mo, "model", None) or None,
        serial=getattr(server_mo, "serial", None) or None,
        system_uuid=getattr(server_mo, "uuid", None) or None,
        nic_macs=_nic_macs(adapter_ifs),
        bmc_address_raw=_bmc_address(mgmt_if),
        bmc_mac=getattr(mgmt_if, "mac", None) if mgmt_if is not None else None,
        site_id=site_id,
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
        attachments=_attachments(adapter_ifs),
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
