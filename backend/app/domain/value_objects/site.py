"""Deriving a server's site from its name.

Every production hostname embeds its site as a whole `-`-delimited token:

    ocp4-prod-one-infra-01          -> one
    ocp4-hypershift-five-01         -> five
    ocp4-hypershift-data-five-02    -> five
    ocp-dell-r660-five-128c-1024gb-FCH123  -> five
    ocp4-one-control-plane-02       -> one

so the name is the authority, not the collector's configuration. That
choice is deliberate: a manager whose `site_id` was set wrong would
otherwise mislabel every server it collects, and nothing downstream could
tell. Parsing the name instead makes the label self-correcting — rename
the host, and the platform agrees on the next collection.

The token must match a `SiteCode` member *exactly* and stand alone
between separators. Substring matching would be actively dangerous here:
the site names are short, common English words, and `ocp4-stone-01`
contains "one" while naming no site at all.

A name with no site token returns `None`. That is a real state the UI
surfaces ("Unassigned"), never a silent default to some arbitrary site —
mislabelling a server's location is worse than admitting the name doesn't
say.
"""

from __future__ import annotations

import re

from app.domain.enums import SiteCode

# Split on any run of the separators these hostnames actually use. Keeping
# this to an explicit character class (rather than `\W+`) means a name in
# an unexpected format yields no tokens and so no site, instead of being
# creatively re-interpreted.
_SEPARATORS = re.compile(r"[-_.]+")

_BY_VALUE = {member.value: member for member in SiteCode}


def parse_site_code(name: str | None) -> SiteCode | None:
    """The `SiteCode` embedded in `name`, or `None` if it holds none.

    Case-insensitive, because hostnames arrive from vendor APIs with
    inconsistent casing. If a name somehow contains two different site
    tokens the result is `None` rather than a guess — an ambiguous name
    is a naming bug worth surfacing, not worth resolving by picking the
    leftmost match.
    """
    if not name:
        return None
    found = {
        _BY_VALUE[token] for token in _SEPARATORS.split(name.strip().lower()) if token in _BY_VALUE
    }
    if len(found) != 1:
        return None
    return found.pop()
