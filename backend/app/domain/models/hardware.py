"""Vendor-neutral hardware model.

Arrays for variable-cardinality hardware (drives, DIMMs, GPUs, PSUs, NICs)
per the platform spec — a server may have 0, 1, or many of any of these,
and nothing here assumes a fixed count.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.enums import MediaType


class Cpu(BaseModel):
    sockets: int = 0
    cores: int = 0
    threads: int = 0
    model: str | None = None


class MemoryModule(BaseModel):
    slot: str | None = None
    size_bytes: int | None = None
    type: str | None = None
    speed_mhz: int | None = None
    serial: str | None = None
    health: str | None = None


class Memory(BaseModel):
    total_bytes: int = 0
    modules: list[MemoryModule] = Field(default_factory=list)


class StorageDrive(BaseModel):
    id: str
    model: str | None = None
    serial: str | None = None
    media_type: MediaType = MediaType.UNKNOWN
    protocol: str | None = None
    capacity_bytes: int | None = None
    slot: str | None = None
    health: str | None = None
    firmware_version: str | None = None
    controller_id: str | None = None


class Storage(BaseModel):
    total_bytes: int = 0
    drives: list[StorageDrive] = Field(default_factory=list)


class Gpu(BaseModel):
    vendor: str | None = None
    model: str | None = None
    serial: str | None = None
    memory_bytes: int | None = None
    health: str | None = None
    pci_address: str | None = None
    firmware_version: str | None = None

    # HBM/GDDR generation (e.g. "HBM3", "HBM3e") — Redfish
    # `Processor.ProcessorMemory[].MemoryType`, distinguishing e.g. an H100
    # from an H200 by memory generation rather than model string alone.
    memory_type: str | None = None
    # `Processor.MemorySummary.ECCModeEnabled`.
    ecc_mode_enabled: bool | None = None
    # `ProcessorMetrics.Correctable/UncorrectableCoreErrorCount` +
    # `.../OtherErrorCount`, summed. DMTF scopes these to "core" and
    # "other" components without specifying which bucket a GPU's own HBM
    # reports under — see docs/adr/0016's update for why both are summed
    # rather than guessed apart.
    correctable_error_count: int | None = None
    uncorrectable_error_count: int | None = None
    # From a `Processor`'s linked `EnvironmentMetrics` resource, not
    # `ProcessorMetrics` — the equivalent fields there are deprecated
    # since Redfish 1.2.
    temperature_celsius: float | None = None
    power_watts: float | None = None


class Psu(BaseModel):
    id: str
    model: str | None = None
    serial: str | None = None
    health: str | None = None
    capacity_watts: int | None = None


class Power(BaseModel):
    psus: list[Psu] = Field(default_factory=list)


class Hardware(BaseModel):
    cpu: Cpu = Field(default_factory=Cpu)
    memory: Memory = Field(default_factory=Memory)
    storage: Storage = Field(default_factory=Storage)
    gpus: list[Gpu] = Field(default_factory=list)
    power: Power = Field(default_factory=Power)
