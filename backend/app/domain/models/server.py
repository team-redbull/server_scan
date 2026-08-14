"""The `servers` collection: one document per physical machine.

`Identity` fields are ordered by correlation strength (see
`app.domain.services.identity`, slice 2) even though the correlation
*algorithm* isn't implemented yet — declaring the field shape now means
identity correlation lands without a schema migration.

`name_normalized`/`serial_normalized`/`model_normalized`/`search_tokens`
are always present with safe defaults (`""` / `[]`), never null: this is
what makes them safe to sort/index on without special-casing missing
values (see `app.domain.services.search`).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.enums import SiteCode, Vendor
from app.domain.models.classification import Classification
from app.domain.models.connectivity import Connectivity
from app.domain.models.hardware import Hardware
from app.domain.models.health import Health
from app.domain.models.maintenance import Maintenance
from app.domain.models.network import NetworkInfo
from app.domain.models.openshift import OpenShiftLifecycle


class Identity(BaseModel):
    # Required, no default: the vendor is a property of which collector
    # produced the record, so it is always known by construction. See
    # `Vendor`'s docstring on why there is no `UNKNOWN` to fall back to.
    vendor: Vendor
    serial: str | None = None
    serial_normalized: str = ""
    system_uuid: str | None = None
    nic_macs: list[str] = Field(default_factory=list)
    external_ids: dict[str, str] = Field(default_factory=dict)  # manager_id -> external id


class ProfileTemplate(BaseModel):
    """The reusable configuration/deployment template this server's
    profile was provisioned from — vendor-neutral, but the underlying
    concept exists (under different names) in every hardware manager this
    platform will eventually integrate with:

    - Cisco UCS Manager: a service profile (`lsServer`) instantiated from
      a **Service Profile Template**, referenced by name via that
      profile's own `srcTemplName` attribute.
    - Cisco Intersight: a `server.Profile` derived from a **Server Profile
      Template** (`server.ProfileTemplate`), referenced via the profile's
      `SrcTemplate` relationship (a `{moid, object_type}` pair).
    - HPE OneView: a Server Profile (`/rest/server-profiles`) derived from
      a **Server Profile Template**, referenced by the profile's
      `serverProfileTemplateUri`.
    - Dell OpenManage Enterprise: a **Deployment Template** (OME's
      Configuration/Deployment Template feature), referenced by
      `TemplateId` at deploy time — OME's public API does not clearly
      expose a persistent "which template was I deployed from" field on
      the device resource itself, unlike the other three, so this may
      stay unpopulated for Dell servers until that's confirmed against a
      live OME instance.

    `name` is always the vendor's own display name for the template — the
    one thing constant across all four platforms. `external_id` is the
    vendor-opaque reference (a template name for UCS Manager, a MoID for
    Intersight, a URI for OneView) — kept as an opaque string rather than
    parsed, the same "store what the vendor gave us, don't overinterpret
    it" approach already used for `Identity.external_ids`. Which platform
    a template reference came from is not duplicated here — it's already
    recoverable via `Server.manager_id` -> the owning `Manager.type`.
    """

    name: str | None = None
    external_id: str | None = None


class Server(BaseModel):
    id: str = Field(alias="_id")
    schema_version: int = 1

    name: str
    name_normalized: str = ""
    model: str | None = None
    model_normalized: str = ""

    # Required rather than defaulted: `Identity.vendor` has no fallback
    # value, so there is no meaningful empty identity to construct.
    identity: Identity
    profile_template: ProfileTemplate = Field(default_factory=ProfileTemplate)
    hardware: Hardware = Field(default_factory=Hardware)
    network: NetworkInfo = Field(default_factory=NetworkInfo)
    connectivity: Connectivity = Field(default_factory=Connectivity)
    classification: Classification = Field(default_factory=Classification)
    health: Health = Field(default_factory=Health)
    maintenance: Maintenance = Field(default_factory=Maintenance)
    openshift: OpenShiftLifecycle = Field(default_factory=OpenShiftLifecycle)

    # Derived from `name` at ingest (`app.domain.value_objects.site`), not
    # taken from the collector's config. `None` means the name carries no
    # site token — surfaced as "Unassigned", never defaulted to a site.
    site_id: SiteCode | None = None
    manager_id: str | None = None

    tags: list[str] = Field(default_factory=list)
    search_tokens: list[str] = Field(default_factory=list)

    source_provider: str | None = None
    last_seen_at: datetime | None = None

    revision: int = 1
    created_at: datetime
    updated_at: datetime

    model_config = {"populate_by_name": True}
