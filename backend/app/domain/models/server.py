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

from app.domain.enums import Vendor
from app.domain.models.classification import Classification
from app.domain.models.connectivity import Connectivity
from app.domain.models.hardware import Hardware
from app.domain.models.health import Health
from app.domain.models.maintenance import Maintenance
from app.domain.models.network import NetworkInfo
from app.domain.models.openshift import OpenShiftLifecycle


class Identity(BaseModel):
    vendor: Vendor = Vendor.UNKNOWN
    serial: str | None = None
    serial_normalized: str = ""
    system_uuid: str | None = None
    nic_macs: list[str] = Field(default_factory=list)
    external_ids: dict[str, str] = Field(default_factory=dict)  # manager_id -> external id


class Server(BaseModel):
    id: str = Field(alias="_id")
    schema_version: int = 1

    name: str
    name_normalized: str = ""
    model: str | None = None
    model_normalized: str = ""

    identity: Identity = Field(default_factory=Identity)
    hardware: Hardware = Field(default_factory=Hardware)
    network: NetworkInfo = Field(default_factory=NetworkInfo)
    connectivity: Connectivity = Field(default_factory=Connectivity)
    classification: Classification = Field(default_factory=Classification)
    health: Health = Field(default_factory=Health)
    maintenance: Maintenance = Field(default_factory=Maintenance)
    openshift: OpenShiftLifecycle = Field(default_factory=OpenShiftLifecycle)

    site_id: str | None = None
    manager_id: str | None = None

    tags: list[str] = Field(default_factory=list)
    search_tokens: list[str] = Field(default_factory=list)

    source_provider: str | None = None
    last_seen_at: datetime | None = None

    revision: int = 1
    created_at: datetime
    updated_at: datetime

    model_config = {"populate_by_name": True}
