"""Deriving a server's site from its name, or failing that from the org
path its service profile lives under.

Every production hostname embeds its site as a whole `-`-delimited token:

    ocp4-prod-tlv-infra-01          -> tlv
    ocp4-hypershift-five-01         -> five
    ocp4-hypershift-data-five-02    -> five
    ocp-dell-r660-five-128c-1024gb-FCH123  -> five
    ocp4-nyc-control-plane-02       -> nyc
    ocp-bat-yam-r660-worker-01      -> bat-yam

so the name is the authority, not the collector's configuration. That
choice is deliberate: a manager whose `site_id` was set wrong would
otherwise mislabel every server it collects, and nothing downstream could
tell. Parsing the name instead makes the label self-correcting — rename
the host, and the platform agrees on the next collection.

The same function reads a UCS org DN (`org-root/org_tlv/ls-worker-01`),
which is the collector's *fallback* when a name carries no site token —
see `app.application.services.ingest`. `/` is a separator here for that
reason.

A token must match a `SiteCode` member *exactly* and stand alone between
separators; a member spelled with a separator (`bat-yam`) matches a run
of consecutive tokens. Substring matching would be actively dangerous
here: the site names are short and common, and `ocp4-stone-01` contains
"one" while naming no site at all.

A name with no site token returns `None`. That is a real state the UI
surfaces ("Unassigned"), never a silent default to some arbitrary site —
mislabelling a server's location is worse than admitting the name doesn't
say.
"""

from __future__ import annotations

import re

from app.domain.enums import SiteCode

# Split on any run of the separators these hostnames and UCS DNs actually
# use. Keeping this to an explicit character class (rather than `\W+`)
# means a name in an unexpected format yields no tokens and so no site,
# instead of being creatively re-interpreted.
_SEPARATORS = re.compile(r"[-_./]+")

_BY_VALUE = {member.value: member for member in SiteCode}

_MAX_TOKENS = max(len(_SEPARATORS.split(value)) for value in _BY_VALUE)

# What a site is called in the UI. Lives beside the enum rather than in
# the API, because the fake seeder writes `Site` documents with the same
# names and the two drifting apart is exactly the sort of thing nothing
# fails on until a human notices the wrong label.
SITE_DISPLAY_NAMES: dict[SiteCode, str] = {
    SiteCode.NYC: "New York City",
    SiteCode.TLV: "Tel Aviv",
    SiteCode.BAT_YAM: "Bat Yam",
    SiteCode.FIVE: "Site Five",
}


def parse_site_code(name: str | None) -> SiteCode | None:
    """The `SiteCode` embedded in `name`, or `None` if it holds none.

    Case-insensitive, because hostnames arrive from vendor APIs with
    inconsistent casing. If a name somehow contains two different site
    tokens the result is `None` rather than a guess — an ambiguous name
    is a naming bug worth surfacing, not worth resolving by picking the
    leftmost match.

    Args:
        name (str | None): A hostname, or a UCS org/profile DN.

    Returns:
        SiteCode | None: The single site named, or `None`.
    """
    if not name:
        return None
    tokens = [token for token in _SEPARATORS.split(name.strip().lower()) if token]
    found = {
        _BY_VALUE[candidate]
        for size in range(1, _MAX_TOKENS + 1)
        for start in range(len(tokens) - size + 1)
        if (candidate := "-".join(tokens[start : start + size])) in _BY_VALUE
    }
    if len(found) != 1:
        return None
    return found.pop()
