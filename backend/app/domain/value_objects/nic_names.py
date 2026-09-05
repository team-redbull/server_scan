"""The FQDD-to-OS-name lookup.

A network interface has two names and neither can be derived from the
other. A BMC reports the hardware one — Dell's FQDD, `NIC.Slot.8-1-1` —
because that is what exists before anything boots. Linux assigns the
other, `ens8f0np0`, from PCI topology once it does. No management API
reports the second, so this platform cannot collect it.

Half of it *looks* derivable and is not, which is the trap this module
exists to avoid guessing around. `NIC.Slot.8` -> `ens8f0np0` follows
systemd's scheme (slot 8, function 0, port 0), but `NIC.Integrated.1` ->
`eno12399np0` does not: that number comes from the onboard device index
in the server's SMBIOS tables and is a property of the model, not of the
FQDD. A rule that derived one and guessed the other would be wrong
exactly where an operator could not tell.

So the mapping is stated, not computed — operator knowledge in the same
sense `INVENTORY_SITES` and `INVENTORY_GPU_MODELS` are, learned once by
booting a host and reading `ip link`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

# `NIC.Slot.8-1-1` -> kind `Slot.8`, port 1. The trailing number is the
# NPAR partition, which never changes the OS name: an unpartitioned port
# and partition 1 of a partitioned one are the same interface.
_FQDD_RE = re.compile(r"^NIC\.([A-Za-z]+\.\d+)-(\d+)-(\d+)$")


class NicNameConfigurationError(ValueError):
    """Raised when `INVENTORY_NIC_OS_NAMES` cannot be parsed."""


@dataclass(frozen=True, slots=True)
class NicNameCatalog:
    """
    OS-level interface names, keyed by FQDD kind and ordered by port.

    Attributes:
        names_by_kind (dict[str, tuple[str, ...]]): `Slot.8` ->
            `("ens8f0np0", "ens8f1np1")`, in port order.
    """

    names_by_kind: dict[str, tuple[str, ...]]

    @classmethod
    def from_spec(cls, spec: str) -> NicNameCatalog:
        """
        Parse the configured spec.

        Args:
            spec (str): `"Slot.8=ens8f0np0,ens8f1np1;Integrated.1=eno12399np0"`.
                Empty means no mapping is known, which is a real state:
                the UI then shows the hardware name alone rather than
                inventing one.

        Returns:
            NicNameCatalog: The parsed catalog.

        Raises:
            NicNameConfigurationError: On an entry with no `=`, an empty
                kind, or no names after it.
        """
        names_by_kind: dict[str, tuple[str, ...]] = {}
        for raw in spec.split(";"):
            entry = raw.strip()
            if not entry:
                continue
            kind, separator, joined = entry.partition("=")
            if not separator:
                raise NicNameConfigurationError(
                    f"{entry!r} is not a 'kind=name,name' entry. Expected e.g. "
                    "'Slot.8=ens8f0np0,ens8f1np1'."
                )
            names = tuple(n.strip() for n in joined.split(",") if n.strip())
            if not kind.strip() or not names:
                raise NicNameConfigurationError(
                    f"{entry!r} needs both an FQDD kind and at least one interface name."
                )
            names_by_kind[kind.strip()] = names
        return cls(names_by_kind=names_by_kind)

    def os_name_for(self, fqdd: str) -> str | None:
        """
        The OS-level name for one hardware interface.

        Args:
            fqdd (str): A Dell FQDD, e.g. `"NIC.Slot.8-1-1"`.

        Returns:
            str | None: The configured name, or `None` when the FQDD is
                not one (an HPE `Physical Port 1`), its kind is not
                configured, or its port is past the configured names —
                each of which means "not known", never a guess.
        """
        match = _FQDD_RE.match(fqdd)
        if match is None:
            return None
        kind, port, _partition = match.groups()
        names = self.names_by_kind.get(kind)
        if names is None:
            return None
        index = int(port) - 1
        return names[index] if 0 <= index < len(names) else None


@lru_cache(maxsize=8)
def nic_name_catalog(spec: str) -> NicNameCatalog:
    """
    A cached catalog for one configured spec.

    Cached like `site_catalog` and `gpu_catalog`, and keyed on the spec
    rather than on `Settings` so a test can pass a literal.

    Args:
        spec (str): The `INVENTORY_NIC_OS_NAMES` value.

    Returns:
        NicNameCatalog: The parsed catalog.

    Raises:
        NicNameConfigurationError: On a malformed entry.
    """
    return NicNameCatalog.from_spec(spec)
