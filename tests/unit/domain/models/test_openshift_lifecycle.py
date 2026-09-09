"""Decoding a membership written before `OpenShiftState` was narrowed."""

from __future__ import annotations

import pytest

from app.domain.enums import OpenShiftState
from app.domain.models.openshift import OpenShiftLifecycle


@pytest.mark.parametrize("retired", ["UPI_NODE", "HOSTED_NODE", "UNKNOWN", "nonsense"])
def test_a_retired_state_decodes_as_available(retired: str) -> None:
    """Without this, every read path breaks against a pre-ADR-0024 database.

    Pydantic rejects an unknown enum member outright; ADR-0024 has what
    that cost when it shipped unhandled.
    """
    state = OpenShiftLifecycle.model_validate({"lifecycle_state": retired})

    assert state.lifecycle_state is OpenShiftState.AVAILABLE


@pytest.mark.parametrize("current", list(OpenShiftState))
def test_a_current_state_is_untouched(current: OpenShiftState) -> None:
    state = OpenShiftLifecycle.model_validate({"lifecycle_state": current.value})

    assert state.lifecycle_state is current
