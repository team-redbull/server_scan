"""The `managers` collection.

`parent_manager_id` models the UCS Central -> UCS Manager hierarchy
observed in the user's existing UCS operator (`BareMetalHostUCS`): it logs
into UCS Central first, reads a service profile's `.domain`, then opens a
second session to that specific UCS Manager domain. Every other manager
type is currently flat (`parent_manager_id=None`), so the field costs
nothing for Dell/HPE/Intersight and models the one real hierarchy Cisco
UCS actually has.

Two separate credential refs (never plaintext values, only secret names —
see spec: "no credentials in source") because the existing operator already
treats "credentials to query the manager" and "credentials to control a
BMC directly" as distinct concerns with different blast radii.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields

# UCS Manager is the only manager type that currently has a real parent
# hierarchy (UCS Central owns UCS Manager domains). Declared explicitly so
# an invalid hierarchy (e.g. an OPENMANAGE manager under a UCS_CENTRAL
# parent) is a validation error, not a silently accepted document.
ALLOWED_PARENT_TYPES: dict[ManagerType, frozenset[ManagerType]] = {
    ManagerType.UCS_MANAGER: frozenset({ManagerType.UCS_CENTRAL}),
}


class Manager(BaseModel):
    id: str = Field(alias="_id")
    name: str
    type: ManagerType
    site_id: str | None = None
    parent_manager_id: str | None = None
    endpoint: str | None = None
    enabled: bool = True
    credential_ref: str | None = None
    bmc_credential_ref: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    audit: AuditFields

    model_config = {"populate_by_name": True}
