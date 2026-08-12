"""Vendor-neutral network model.

MAC addresses stored here are always already normalized
(`app.domain.value_objects.normalize_mac`) before a document is persisted —
callers building a `Server` construct these sub-models from normalized
values, they don't normalize inside the model itself, so a model
constructed directly in a test can also exercise un-normalized input
without a validator fighting it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.enums import LinkState


class BmcInfo(BaseModel):
    address_raw: str | None = None
    scheme: str | None = None
    host: str | None = None
    host_is_ip: bool = False
    port: int | None = None
    path: str | None = None
    mac: str | None = None


class NetworkInterface(BaseModel):
    name: str
    mac: str | None = None
    speed_mbps: int | None = None
    link_state: LinkState = LinkState.UNKNOWN


class NetworkInfo(BaseModel):
    bmc: BmcInfo = Field(default_factory=BmcInfo)
    interfaces: list[NetworkInterface] = Field(default_factory=list)
