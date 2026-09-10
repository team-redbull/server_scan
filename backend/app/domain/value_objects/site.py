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

A code spelled with a separator (`bat-yam`) matches a run of consecutive
tokens, exactly. Every code and alias also matches as a **substring of a
single token** — `ocp4-computezn-01` matches an alias `zn`, glued
together with no separator of its own — a deliberate reversal, made at
the operator's explicit request 2026-09-09, of this module's original
design: matching used to require a token to *equal* a code outright,
specifically to reject `ocp4-tlvx-01` "containing" `tlv` while naming no
site at all. That false-positive risk is now accepted, consciously, in
exchange for letting a short alias (`zn`, `fn`) match wherever it appears
glued into a name. A name whose tokens resolve to two *different* sites
is still `None` rather than a guess (see `SiteCatalog.parse`) — that
safety net is unchanged, and matters more now that substrings match more
often.

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

# Wire spelling for a server whose name carries no site token; stored as
# `None` on the document.
UNASSIGNED_SITE_ID = "unassigned"


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
        code (str): The canonical token embedded in hostnames, e.g.
            `"bat-yam"`. Also the `Site` document's id and the value
            stored in `Server.site_id` — an alias never is, even when a
            server's own name carried the alias rather than this code.
        name (str): What the UI shows, e.g. `"Bat Yam"`.
        aliases (tuple[str, ...]): Other tokens a hostname may carry that
            mean this same site — e.g. an old naming convention's code, or
            an abbreviation a different team used. Every server matching
            an alias is stored and shown under `code`, not the alias, so
            they combine onto one site card rather than splitting across
            two.
    """

    code: str
    name: str
    aliases: tuple[str, ...] = ()


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

        The code half may itself be `|`-separated aliases, e.g.
        `znif|prep:Znif` — the first token is canonical (`Server.site_id`,
        every URL, the `Site` document's own id), the rest are other
        tokens a hostname may carry for that same site, so a naming
        convention that changed over time — or two teams' different
        abbreviations for one site — still combines onto one site card
        rather than splitting across two.

        Args:
            spec (str): The raw configured value. Empty means the shipped
                default rather than "no sites", since a deployment with no
                sites could label nothing at all.

        Returns:
            SiteCatalog: The parsed catalog, in configured order.

        Raises:
            SiteConfigurationError: On a malformed or duplicate entry —
                including one token (a code or an alias) reused anywhere
                else in the spec, which would make it ambiguous which
                site a hostname carrying it names.
        """
        text = spec.strip() or DEFAULT_SITES_SPEC
        definitions: list[SiteDefinition] = []
        seen: set[str] = set()
        for entry in text.split(","):
            if not entry.strip():
                continue
            code_field, _, name = entry.partition(":")
            tokens = [token.strip().lower() for token in code_field.split("|")]
            name = name.strip()
            for token in tokens:
                if not _VALID_CODE.match(token):
                    raise SiteConfigurationError(
                        f"INVENTORY_SITES: {token!r} is not a usable site code or alias. "
                        "A code is the token that appears inside a hostname, so it must "
                        "be lowercase letters, digits and single hyphens — e.g. 'tlv' or "
                        "'bat-yam'."
                    )
                if token in seen:
                    raise SiteConfigurationError(
                        f"INVENTORY_SITES: {token!r} is listed twice — as a code or alias "
                        "of more than one site, or twice for the same one."
                    )
                seen.add(token)
            code, *aliases = tokens
            definitions.append(
                SiteDefinition(code=code, name=name or _title_case(code), aliases=tuple(aliases))
            )
        if not definitions:
            raise SiteConfigurationError(
                "INVENTORY_SITES is set but lists no sites. Leave it unset for the "
                f"default ({DEFAULT_SITES_SPEC!r}), or name at least one site."
            )
        return cls(definitions=tuple(definitions))

    @property
    def codes(self) -> tuple[str, ...]:
        """
        Every configured site code, in configured order.

        Returns:
            tuple[str, ...]: Every site code, in configured order.
        """
        return tuple(definition.code for definition in self.definitions)

    def __contains__(self, code: object) -> bool:
        """
        Whether this deployment knows about a candidate site code.

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
        Every site code and alias as one regex alternation.

        No production caller left as of 2026-09-08 — the seeded
        classification rules that used to interpolate this were broadened
        into plain prefix/substring catch-alls with no site token at all
        (`app.infrastructure.mongodb.classification_rule_repository.
        default_system_rules`). Kept, and kept correct, for whatever next
        needs "every token that means a configured site" as one pattern.

        Returns:
            str: e.g. `"nyc|tlv|bat-yam|five"`, regex-escaped.
        """
        return "|".join(
            re.escape(token)
            for definition in self.definitions
            for token in (definition.code, *definition.aliases)
        )

    def parse(self, name: str | None) -> str | None:
        """
        The site code embedded in `name`, or `None` if it holds none.

        Case-insensitive, because hostnames arrive from vendor APIs with
        inconsistent casing. **Canonical codes are tried first, aliases
        only if that finds nothing at all** — added 2026-09-10, after a
        real collision: with `znif|prep:Znif` and `five:Site Five`
        both configured, `ocp4-prep-five-compute-01` carries `five` (a
        real site's own code) and `prep` (someone else's alias) at once.
        Treating both tiers as one pool made that name ambiguous — two
        sites "matched" — and dropped it to Unassigned, even though
        `five` alone is exactly what a name with no alias in it would
        have resolved to. A canonical code is never in question the way
        an alias can be, so it wins outright; aliases are consulted only
        when no real code named anything, which is the case they exist
        for (`ocp4-prep-compute-01`, no `five`/`znif`/... token at all,
        still resolves through `prep` to `znif`).

        Two ways a code or alias can match, within whichever tier is
        being tried:

        1. As a run of one or more whole, consecutive tokens, exactly —
           what makes a multi-token code (`bat-yam`) match its own
           separator-spelled form.
        2. As a **substring of a single token**, glued in with no
           separator of its own (`ocp4-computezn-01` matches an alias
           `zn`) — a deliberate reversal, 2026-09-09, of matching only
           whole tokens; see the module docstring for why and what it
           trades away.

        Two *real codes* named at once is still `None` rather than a
        guess — that tier's ambiguity is a naming bug worth surfacing,
        never resolved by picking a side. **Two different *aliases* named
        at once picks the leftmost one instead** (2026-09-10, at the
        operator's request, reversing what this docstring said until
        then): `fn`/`prep` both configured, `fn-data-prep-ocp-compute-01`
        resolves through `fn` (`five`), not `None` — no real code is ever
        in play here to make the pick actually risky the way it would be
        for two real codes. A name carrying two different tokens (or
        substrings) that both alias the *same* site is not ambiguous
        either way — it resolves to that one canonical code, same as
        repeating the code itself would.

        Args:
            name (str | None): A hostname, or a UCS org/profile DN.

        Returns:
            str | None: The single canonical site code named, or `None`.
                Always `definition.code`, never an alias, even when the
                alias is what the name actually carried.
        """
        if not name:
            return None
        tokens = [token for token in _SEPARATORS.split(name.strip().lower()) if token]

        codes_only = {definition.code: definition.code for definition in self.definitions}
        found = self._matches(tokens, codes_only)
        if len(found) == 1:
            return next(iter(found))
        if found:
            return None  # 2+ real codes named at once — ambiguous, no alias tier can help

        with_aliases = {
            token: definition.code
            for definition in self.definitions
            for token in (definition.code, *definition.aliases)
        }
        found = self._matches(tokens, with_aliases)
        if not found:
            return None
        # 2+ aliases at once picks whichever matched the earliest token —
        # see the docstring above for why this is safe where two real
        # codes at once deliberately is not.
        return min(found.items(), key=lambda item: item[1])[0]

    @staticmethod
    def _matches(tokens: list[str], by_value: dict[str, str]) -> dict[str, int]:
        """
        Every site `tokens` names, against one code/alias -> code mapping.

        Args:
            tokens (list[str]): The hostname's own `-`/`_`/`.`/`/`-split
                tokens, already lowercased.
            by_value (dict[str, str]): Candidate token -> canonical code,
                scoped by the caller to just codes or codes-plus-aliases.

        Returns:
            dict[str, int]: Canonical code -> the earliest token index it
                matched at. Empty (nothing matched), one entry (a clean
                result) or 2+ (ambiguous within this tier — `parse`
                decides what "ambiguous" means for the tier it called
                this with).
        """
        max_tokens = max((len(_SEPARATORS.split(code)) for code in by_value), default=1)
        positions: dict[str, int] = {}
        for size in range(1, max_tokens + 1):
            for start in range(len(tokens) - size + 1):
                candidate = "-".join(tokens[start : start + size])
                code = by_value.get(candidate)
                if code is not None:
                    positions[code] = min(positions.get(code, start), start)
        # A code/alias appearing anywhere inside one token, not just a
        # token that equals it outright — e.g. "zn" inside "computezn". A
        # multi-token code (containing its own "-") can never be a
        # substring of one token, since splitting already removed every
        # "-" from each token, so this only ever fires for single-token
        # codes/aliases — exactly the short ones this exists for.
        for index, hostname_token in enumerate(tokens):
            for candidate, code in by_value.items():
                if candidate in hostname_token:
                    positions[code] = min(positions.get(code, index), index)
        return positions


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
