"""Guards that the frontend's copies of `ManagerType` do not drift from
the backend's.

The frontend has no way to learn the manager types at runtime — unlike
sites, which it reads from `GET /api/v1/sites` — so it carries hardcoded
copies. That is a defensible choice: a new `ManagerType` always requires
a provider implementation anyway, so it can never appear without a code
change. What is *not* defensible is the copies silently falling behind,
and they did: the Intersight collector shipped and its value was missing
from the inventory page's Source filter for six commits, which made a
whole collector's servers unfilterable in the UI with no error anywhere.

These read the actual `.tsx`/`.ts` sources rather than a generated
artifact, in the same spirit as `test_no_committed_secrets.py` — a
string-level check across the language boundary is worth more than the
elegance it costs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tools.run_collector import PROVIDER_FACTORIES

from app.domain.enums import ManagerType

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]

# Derived, never restated. Manager types with a collector, so a server can
# really carry them in `source_provider`; filtering by a type with no
# implementation would always return nothing, which is why the Source
# filter lists only these.
#
# This used to be a hand-written set here, and it drifted exactly like the
# frontend list it guards: OPENMANAGE and ONEVIEW shipped, nobody updated
# it, and the guard went green while Dell and HPE servers were
# unfilterable in the UI. A guard that restates the fact it protects
# protects nothing.
_IMPLEMENTED = frozenset(PROVIDER_FACTORIES)


def _source(relative: str) -> str:
    """
    Read one frontend source file.

    Args:
        relative (str): Repo-relative path.

    Returns:
        str: Its text.
    """
    path = _REPO / relative
    assert path.exists(), f"{relative} has moved; update this guard rather than deleting it."
    return path.read_text()


def test_the_source_filter_offers_every_implemented_collector() -> None:
    """A collector whose servers cannot be filtered for is a collector
    whose servers are hard to find at all.
    """
    text = _source("frontend/src/api/sites.ts")
    listed = set(re.findall(r'\{\s*value:\s*"([A-Z_]+)"', text))

    missing = {t.value for t in _IMPLEMENTED} - listed
    assert not missing, (
        f"SOURCE_PROVIDERS in frontend/src/api/sites.ts is missing {sorted(missing)}. "
        "A collector was implemented without being added to the inventory page's "
        "Source filter."
    )


def test_the_source_filter_offers_nothing_unimplemented() -> None:
    """The other direction: offering a filter that can only ever return
    an empty page reads as a broken page, not as an unbuilt collector.
    """
    text = _source("frontend/src/api/sites.ts")
    listed = set(re.findall(r'\{\s*value:\s*"([A-Z_]+)"', text))
    known = {t.value for t in ManagerType}

    unimplemented = (listed & known) - {t.value for t in _IMPLEMENTED}
    assert not unimplemented, (
        f"SOURCE_PROVIDERS offers {sorted(unimplemented)}, which has no collector, "
        "so selecting it always returns nothing."
    )


@pytest.mark.parametrize(
    "relative",
    [
        "frontend/src/types/classification.ts",
        "frontend/src/features/classification/RuleEditorPage.tsx",
        "frontend/src/features/health/PolicyEditorPage.tsx",
    ],
)
def test_scope_pickers_carry_every_manager_type(relative: str) -> None:
    """Scoping a rule or policy is about *configuration*, not about what
    has been collected yet, so these list every `ManagerType` — including
    the ones with no collector, which an operator may legitimately want a
    policy ready for.

    `REDFISH_STANDALONE` was missing from all three of these, which meant
    no rule or policy could be scoped to the standalone Redfish collector
    at all.
    """
    text = _source(relative)
    missing = [member.value for member in ManagerType if f'"{member.value}"' not in text]

    assert not missing, (
        f"{relative} is missing manager type(s) {missing}. Add them there, or narrow this "
        "guard deliberately if a type is meant to be unselectable."
    )
