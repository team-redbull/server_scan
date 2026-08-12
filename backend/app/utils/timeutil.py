"""Timezone-aware clock utilities.

Every stored timestamp must be timezone-aware UTC. Naive datetimes are how
"why is this event 3 hours off" bugs get into production; centralizing
`utcnow()` here (instead of `datetime.utcnow()`, which returns a naive
datetime) is the whole fix.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
