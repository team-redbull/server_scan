"""Timezone-aware clock utilities.

Every stored timestamp must be timezone-aware UTC. Naive datetimes are how
"why is this event 3 hours off" bugs get into production; centralizing
`utcnow()` here (instead of `datetime.utcnow()`, which returns a naive
datetime) is the whole fix.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC `datetime`.

    Returns:
        datetime: The current instant, in UTC.
    """
    return datetime.now(UTC)
