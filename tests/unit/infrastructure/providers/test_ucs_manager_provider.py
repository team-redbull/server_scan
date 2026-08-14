"""`app.infrastructure.providers.ucs_manager.provider` — just the pure
`_is_equipped` presence filter here. The rest of `UcsManagerProvider`
does real XML API calls through `UcsManagerClient` and needs a live or
mocked UCS Manager domain to exercise meaningfully — out of scope for a
unit test; verify that end-to-end against Cisco's UCS Platform Emulator
instead.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.infrastructure.providers.ucs_manager.provider import _is_equipped

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("presence", "expected"),
    [
        ("equipped", True),
        ("equipped-deprecated", True),
        ("equipped-identity-unestablishable", True),
        ("equipped-with-malformed-fru", True),
        ("empty", False),
        ("missing", False),
        ("mismatch", False),
        ("unauthorized", False),
        ("unknown", False),
        ("inaccessible", False),
        (None, False),
        ("", False),
    ],
)
def test_is_equipped(presence: str | None, expected: bool) -> None:
    mo = SimpleNamespace(presence=presence)
    assert _is_equipped(mo) is expected
