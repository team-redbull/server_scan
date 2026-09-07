"""Value objects: immutable, self-validating domain types with no identity of their own."""

from app.domain.value_objects.bmc_address import BmcAddress, parse_bmc_address
from app.domain.value_objects.mac_address import normalize_mac

__all__ = ["BmcAddress", "normalize_mac", "parse_bmc_address"]
