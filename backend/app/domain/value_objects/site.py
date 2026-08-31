"""The set of sites, and deriving a server's site from its name.

**The sites are deployment configuration, not code.** Which site codes
exist is a property of one estate's hostname convention — `tlv` means
something in this deployment and nothing in the next — so the set is
loaded from `INVENTORY_SITES` and parsed into a `SiteCatalog` at startup.
Renaming a site, adding one, or standing the platform up for a different
estate is an environment change, never an edit to this file. See
docs/adr/0018-sites-from-configuration.md.

Every production hostname embeds its site as a whole `-`-delimited token:

    ocp4-prod-tlv-infra-01          -> tlv
    ocp4-hypershift-five-01         -> five
    ocp4-hypershift-data-five-02    -> five
    ocp-dell-r660-five-128c-1024gb-FCH123  -> five
    ocp4-nyc-control-plane-02       -> nyc
    ocp-bat-yam-r660-worker-01      -> bat-yam

so the name is the authority, not the collector's configuration. That
choice is deliberate: a manager whose site was set wrong would otherwise
mislabel every server it collects, and nothing downstream could tell.
Parsing the name instead makes the label self-correcting — rename the
host, and the platform agrees on the next collection.

The same function reads a UCS org DN (`org-root/org_tlv/ls-worker-01`),
which is the collector's *fallback* when a name carries no site token —
see `app.application.services.ingest`. `/` is a separator here for that
reason.

A token must match a site code *exactly* and stand alone between
separators; a code spelled with a separator (`bat-yam`) matches a run of
consecutive tokens. Substring matching would be actively dangerous here:
site codes are short, and `ocp4-tlvx-01` contains "tlv" while naming no
site at all.

A name with no site token returns `None`. That is a real state the UI
surfaces ("Unassigned"), never a silent default to some arbitrary site —
mislabelling a server's location is worse than admitting the name
doesn't say.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

# Split on any run of the separators these hostnames and UCS DNs actually
# use. Keeping this to an explicit character class (rather than `\W+`)
# means a name in an unexpected format yields no tokens and so no site,
# instead of being creatively re-interpreted.
_SEPARATORS = re.compile(r"[-_./]+")

# A site code is what appears inside a hostname, so it is restricted to
# what a hostname can carry. Rejected loudly at startup rather than
# silently never matching anything.
_VALID_CODE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# The shipped default, and the set this platform was built against. It is
# a default rather than a requirement: a deployment sets INVENTORY_SITES
# and never edits this file.
DEFAULT_SITES_SPEC = "nyc:New York City,tlv:Tel Aviv,bat-yam:Bat Yam,five:Site Five"


class SiteConfigurationError(ValueError):
    """`INVENTORY_SITES` could not be read.

    Raised at startup, never during a request: a typo in the site list
    changes which servers get a site at all, so it has to fail loudly
    while someone is still looking at the deployment.
    """


@dataclass(frozen=True, slots=True)
class SiteDefinition:
    """
    One site.

    Attributes:
        code (str): The token embedded in hostnames, e.g. `"bat-yam"`.
            Also the `Site` document's id and the value stored in
            `Server.site_id`.
        name (str): What the UI shows, e.g. `"Bat Yam"`.
    """

    code: str
    name: str


@dataclass(frozen=True, slots=True)
class SiteCatalog:
    """
    The sites this deployment knows about.

    Closed at runtime, but its contents come from configuration rather
    than from source — which is the whole point. Immutable once built, so
    it can be shared freely and cannot drift mid-run.
    """

    definitions: tuple[SiteDefinition, ...]

    @classmethod
    def from_spec(cls, spec: str) -> SiteCatalog:
        """
        Parse `INVENTORY_SITES` into a catalog.

        The format is `code:Display Name`, comma-separated:

            nyc:New York City,tlv:Tel Aviv,bat-yam:Bat Yam

        The display half is optional — `nyc,tlv` gives codes with
        title-cased names — because a deployment that does not care about
        pretty labels should not have to invent them.

        Args:
            spec (str): The raw configured value. Empty means the shipped
                default rather than "no sites", since a deployment with no
                sites could label nothing at all.

        Returns:
            SiteCatalog: The parsed catalog, in configured order.

        Raises:
            SiteConfigurationError: On a malformed or duplicate entry.
        """
        text = spec.strip() or DEFAULT_SITES_SPEC
        definitions: list[SiteDefinition] = []
        seen: set[str] = set()
        for entry in text.split(","):
            if not entry.strip():
                continue
            code, _, name = entry.partition(":")
            code = code.strip().lower()
            name = name.strip()
            if not _VALID_CODE.match(code):
                raise SiteConfigurationError(
                    f"INVENTORY_SITES: {code!r} is not a usable site code. A code is the "
                    "token that appears inside a hostname, so it must be lowercase "
                    "letters, digits and single hyphens — e.g. 'tlv' or 'bat-yam'."
                )
            if code in seen:
                raise SiteConfigurationError(
                    f"INVENTORY_SITES: site code {code!r} is listed twice."
                )
            seen.add(code)
            definitions.append(SiteDefinition(code=code, name=name or _title_case(code)))
        if not definitions:
            raise SiteConfigurationError(
                "INVENTORY_SITES is set but lists no sites. Leave it unset for the "
                f"default ({DEFAULT_SITES_SPEC!r}), or name at least one site."
            )
        return cls(definitions=tuple(definitions))

    @property
    def codes(self) -> tuple[str, ...]:
        """
        Returns:
            tuple[str, ...]: Every site code, in configured order.
        """
        return tuple(definition.code for definition in self.definitions)

    def __contains__(self, code: object) -> bool:
        """
        Args:
            code (object): A candidate site code.

        Returns:
            bool: Whether this deployment knows that site.
        """
        return isinstance(code, str) and code.lower() in set(self.codes)

    def name_for(self, code: str) -> str:
        """
        What to call a site in the UI.

        Args:
            code (str): A site code.

        Returns:
            str: Its display name, or a title-cased fallback for a code
                this catalog does not know — which happens to a server
                stored under a site that has since been reconfigured
                away, and is better rendered than hidden.
        """
        for definition in self.definitions:
            if definition.code == code:
                return definition.name
        return _title_case(code)

    def alternation(self) -> str:
        """
        Every site code as one regex alternation.

        Used to build the seeded classification rules, so adding a site to
        the configuration cannot leave those patterns behind.

        Returns:
            str: e.g. `"nyc|tlv|bat-yam|five"`, regex-escaped.
        """
        return "|".join(re.escape(code) for code in self.codes)

    def parse(self, name: str | None) -> str | None:
        """
        The site code embedded in `name`, or `None` if it holds none.

        Case-insensitive, because hostnames arrive from vendor APIs with
        inconsistent casing. If a name somehow contains two different site
        tokens the result is `None` rather than a guess — an ambiguous
        name is a naming bug worth surfacing, not worth resolving by
        picking the leftmost match.

        Args:
            name (str | None): A hostname, or a UCS org/profile DN.

        Returns:
            str | None: The single site code named, or `None`.
        """
        if not name:
            return None
        by_value = {definition.code: definition.code for definition in self.definitions}
        max_tokens = max((len(_SEPARATORS.split(code)) for code in by_value), default=1)
        tokens = [token for token in _SEPARATORS.split(name.strip().lower()) if token]
        found = {
            by_value[candidate]
            for size in range(1, max_tokens + 1)
            for start in range(len(tokens) - size + 1)
            if (candidate := "-".join(tokens[start : start + size])) in by_value
        }
        if len(found) != 1:
            return None
        return found.pop()


def _title_case(code: str) -> str:
    """
    A readable label for a code with no configured name.

    Args:
        code (str): A site code, e.g. `"bat-yam"`.

    Returns:
        str: e.g. `"Bat Yam"`.
    """
    return " ".join(part.capitalize() for part in code.split("-") if part)


@lru_cache(maxsize=8)
def site_catalog(spec: str) -> SiteCatalog:
    """
    A cached catalog for one configured spec.

    Cached because the API builds one per request from the same settings
    string, and parsing it every time would be pure waste. Keyed on the
    spec itself rather than on `Settings`, so a test can pass a literal.

    Args:
        spec (str): The `INVENTORY_SITES` value.

    Returns:
        SiteCatalog: The parsed catalog.

    Raises:
        SiteConfigurationError: On a malformed entry.
    """
    return SiteCatalog.from_spec(spec)


def parse_site_code(name: str | None, catalog: SiteCatalog) -> str | None:
    """
    The site code embedded in `name`, against a given catalog.

    A free function as well as a method because the collectors read it
    that way, and because passing the catalog explicitly is what keeps
    this module free of any dependency on application configuration.

    Args:
        name (str | None): A hostname, or a UCS org/profile DN.
        catalog (SiteCatalog): The sites this deployment knows.

    Returns:
        str | None: The single site code named, or `None`.
    """
    return catalog.parse(name)
