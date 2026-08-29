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
        ("ocp4-prod-tlv-infra-01", SiteCode.TLV),
        ("ocp4-prep-five-compute-01", SiteCode.FIVE),
        ("ocp4-hypershift-five-01", SiteCode.FIVE),
        ("ocp4-hypershift-data-five-02", SiteCode.FIVE),
        ("ocp-dell-r660-five-128c-1024gb-FCH1234567", SiteCode.FIVE),
        ("ocp4-five-compute-01", SiteCode.FIVE),
        ("ocp4-nyc-control-plane-02", SiteCode.NYC),
        ("ocp4-tlv-worker-03", SiteCode.TLV),
        # A site code that is itself two tokens long.
        ("ocp4-bat-yam-infra-01", SiteCode.BAT_YAM),
        ("ocp-dell-r660-bat-yam-64c-512gb-FCH1234567", SiteCode.BAT_YAM),
        # Vendor APIs are inconsistent about case.
        ("OCP4-PROD-TLV-INFRA-01", SiteCode.TLV),
        ("Ocp4-Prod-Nyc-Infra-01", SiteCode.NYC),
        # Other separators seen in hostnames.
        ("ocp4_prod_tlv_infra_01", SiteCode.TLV),
        ("ocp4.prod.nyc.infra.01", SiteCode.NYC),
        ("  ocp4-prod-tlv-infra-01  ", SiteCode.TLV),
        # A UCS org DN, which is the collector's fallback when the name
        # carries no site token.
        ("org-root/org_tlv/ls-worker-01", SiteCode.TLV),
        ("org-root/org-bat-yam/ls-worker-01", SiteCode.BAT_YAM),
    ],
)
def test_parses_the_site_token(name: str, expected: SiteCode) -> None:
    assert parse_site_code(name) is expected


@pytest.mark.parametrize(
    "name",
    [
        # The whole reason this is token-based and not a substring search:
        # every one of these CONTAINS a site name but names no site.
        "ocp4-tlvx-01",  # contains "tlv"
        "ocp4-prod-nycity-01",  # contains "nyc"
        "ocp4-fivestar-01",  # contains "five"
        "ocp4-batyam-01",  # "bat-yam" without its separator
        "ocp4-bat-01",  # half of "bat-yam"
        "batman-host",
    ],
)
def test_does_not_match_a_site_name_embedded_in_a_larger_word(name: str) -> None:
    assert parse_site_code(name) is None


@pytest.mark.parametrize(
    "name", ["", None, "   ", "ocp4-prod-infra-01", "server-01", "-", "___", "org-root/ls-w-01"]
)
def test_returns_none_when_there_is_no_site_token(name: str | None) -> None:
    assert parse_site_code(name) is None


def test_ambiguous_name_with_two_sites_returns_none_rather_than_guessing() -> None:
    assert parse_site_code("ocp4-tlv-nyc-infra-01") is None


def test_the_same_site_repeated_is_not_ambiguous() -> None:
    assert parse_site_code("ocp4-tlv-infra-tlv-01") is SiteCode.TLV


def test_every_site_code_is_parseable_from_a_realistic_name() -> None:
    """Guards against a SiteCode being added to the enum without the
    parser being able to produce it.
    """
    for member in SiteCode:
        assert parse_site_code(f"ocp4-prod-{member.value}-infra-01") is member
