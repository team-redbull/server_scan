"""`app.domain.value_objects.site.parse_site_code` — the function every
server's site label now depends on, so its false-positive behaviour
matters more than its happy path.
"""

from __future__ import annotations

import pytest

from app.domain.enums import SiteCode
from app.domain.value_objects.site import parse_site_code

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # The real production naming patterns.
        ("ocp4-prod-one-infra-01", SiteCode.ONE),
        ("ocp4-prep-five-compute-01", SiteCode.FIVE),
        ("ocp4-hypershift-five-01", SiteCode.FIVE),
        ("ocp4-hypershift-data-five-02", SiteCode.FIVE),
        ("ocp-dell-r660-five-128c-1024gb-FCH1234567", SiteCode.FIVE),
        ("ocp4-five-compute-01", SiteCode.FIVE),
        ("ocp4-one-control-plane-02", SiteCode.ONE),
        ("ocp4-two-worker-03", SiteCode.TWO),
        ("ocp4-three-infra-01", SiteCode.THREE),
        ("ocp4-four-compute-09", SiteCode.FOUR),
        # Vendor APIs are inconsistent about case.
        ("OCP4-PROD-ONE-INFRA-01", SiteCode.ONE),
        ("Ocp4-Prod-Two-Infra-01", SiteCode.TWO),
        # Other separators seen in hostnames.
        ("ocp4_prod_one_infra_01", SiteCode.ONE),
        ("ocp4.prod.three.infra.01", SiteCode.THREE),
        ("  ocp4-prod-one-infra-01  ", SiteCode.ONE),
    ],
)
def test_parses_the_site_token(name: str, expected: SiteCode) -> None:
    assert parse_site_code(name) is expected


@pytest.mark.parametrize(
    "name",
    [
        # The whole reason this is token-based and not a substring search:
        # every one of these CONTAINS a site name but names no site.
        "ocp4-stone-01",  # contains "one"
        "ocp4-prod-money-01",  # contains "one"
        "ocp4-atone-infra-01",  # contains "one"
        "ocp4-fivestar-01",  # contains "five"
        "ocp4-twofold-01",  # contains "two"
        "ocp4-threshold-01",  # contains "three"? no — but adjacent
        "onerous-host",
    ],
)
def test_does_not_match_a_site_name_embedded_in_a_larger_word(name: str) -> None:
    assert parse_site_code(name) is None


@pytest.mark.parametrize("name", ["", None, "   ", "ocp4-prod-infra-01", "server-01", "-", "___"])
def test_returns_none_when_there_is_no_site_token(name: str | None) -> None:
    assert parse_site_code(name) is None


def test_ambiguous_name_with_two_sites_returns_none_rather_than_guessing() -> None:
    assert parse_site_code("ocp4-one-two-infra-01") is None


def test_the_same_site_repeated_is_not_ambiguous() -> None:
    assert parse_site_code("ocp4-one-infra-one-01") is SiteCode.ONE


def test_every_site_code_is_parseable_from_a_realistic_name() -> None:
    """Guards against a SiteCode being added to the enum without the
    parser being able to produce it.
    """
    for member in SiteCode:
        assert parse_site_code(f"ocp4-prod-{member.value}-infra-01") is member
