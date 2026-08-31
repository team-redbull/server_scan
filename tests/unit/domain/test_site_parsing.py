"""`app.domain.value_objects.site` — the site catalog, and the parser
every server's site label depends on.

Its false-positive behaviour matters more than its happy path: the codes
are short, they appear inside hostnames, and a substring match would
label servers with a site they are not in.

Since sites became configuration rather than an enum
(docs/adr/0018-sites-from-configuration.md), the second thing these tests
guard is that a *reconfigured* catalog behaves — a deployment naming its
own sites is the normal case, not an edge one.
"""

from __future__ import annotations

import pytest

from app.domain.value_objects.site import (
    DEFAULT_SITES_SPEC,
    SiteCatalog,
    SiteConfigurationError,
    parse_site_code,
    site_catalog,
)

pytestmark = pytest.mark.unit

SITES = site_catalog(DEFAULT_SITES_SPEC)


def parse(name: str | None) -> str | None:
    """
    Parse against the shipped default catalog.

    Args:
        name (str | None): A hostname or DN.

    Returns:
        str | None: The site code, or None.
    """
    return parse_site_code(name, SITES)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # The real production naming patterns.
        ("ocp4-prod-tlv-infra-01", "tlv"),
        ("ocp4-prep-five-compute-01", "five"),
        ("ocp4-hypershift-five-01", "five"),
        ("ocp4-hypershift-data-five-02", "five"),
        ("ocp-dell-r660-five-128c-1024gb-FCH1234567", "five"),
        ("ocp4-five-compute-01", "five"),
        ("ocp4-nyc-control-plane-02", "nyc"),
        ("ocp4-tlv-worker-03", "tlv"),
        # A site code that is itself two tokens long.
        ("ocp4-bat-yam-infra-01", "bat-yam"),
        ("ocp-dell-r660-bat-yam-64c-512gb-FCH1234567", "bat-yam"),
        # Vendor APIs are inconsistent about case.
        ("OCP4-PROD-TLV-INFRA-01", "tlv"),
        ("Ocp4-Prod-Nyc-Infra-01", "nyc"),
        # Other separators seen in hostnames.
        ("ocp4_prod_tlv_infra_01", "tlv"),
        ("ocp4.prod.nyc.infra.01", "nyc"),
        ("  ocp4-prod-tlv-infra-01  ", "tlv"),
        # A UCS org DN, which is the collector's fallback when the name
        # carries no site token.
        ("org-root/org_tlv/ls-worker-01", "tlv"),
        ("org-root/org-bat-yam/ls-worker-01", "bat-yam"),
    ],
)
def test_parses_the_site_token(name: str, expected: str) -> None:
    assert parse(name) == expected


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
    assert parse(name) is None


@pytest.mark.parametrize(
    "name", ["", None, "   ", "ocp4-prod-infra-01", "server-01", "-", "___", "org-root/ls-w-01"]
)
def test_returns_none_when_there_is_no_site_token(name: str | None) -> None:
    assert parse(name) is None


def test_ambiguous_name_with_two_sites_returns_none_rather_than_guessing() -> None:
    assert parse("ocp4-tlv-nyc-infra-01") is None


def test_the_same_site_repeated_is_not_ambiguous() -> None:
    assert parse("ocp4-tlv-infra-tlv-01") == "tlv"


def test_every_configured_site_is_parseable_from_a_realistic_name() -> None:
    """Guards against a site being configured that the parser can never
    actually produce — which would leave that site's servers unassigned
    with no error anywhere.
    """
    for code in SITES.codes:
        assert parse(f"ocp4-prod-{code}-infra-01") == code


def test_every_configured_site_has_a_display_name() -> None:
    """`GET /api/v1/sites` is the only place the UI learns site names."""
    for definition in SITES.definitions:
        assert definition.name


# --- the catalog itself, now that sites are configuration -------------


def test_a_deployment_can_name_its_own_sites() -> None:
    """The whole point: an operator changes one environment variable and
    the platform speaks their estate's vocabulary, with no code change.
    """
    catalog = SiteCatalog.from_spec("lon:London,fra:Frankfurt")

    assert catalog.codes == ("lon", "fra")
    assert catalog.name_for("lon") == "London"
    assert parse_site_code("ocp4-prod-lon-infra-01", catalog) == "lon"
    # And the sites it no longer has are no longer recognised.
    assert parse_site_code("ocp4-prod-tlv-infra-01", catalog) is None


def test_the_display_half_is_optional() -> None:
    """A deployment that does not care about pretty labels should not
    have to invent them.
    """
    catalog = SiteCatalog.from_spec("lon,bat-yam")

    assert catalog.name_for("lon") == "Lon"
    assert catalog.name_for("bat-yam") == "Bat Yam"


def test_an_empty_spec_falls_back_to_the_shipped_default() -> None:
    """So dev and CI need configure nothing."""
    assert SiteCatalog.from_spec("").codes == SITES.codes
    assert SiteCatalog.from_spec("   ").codes == SITES.codes


def test_a_multi_token_configured_code_still_matches_consecutive_tokens() -> None:
    """The `bat-yam` shape has to keep working for a site nobody
    anticipated when the parser was written.
    """
    catalog = SiteCatalog.from_spec("new-york-city:NYC")

    assert parse_site_code("ocp4-new-york-city-infra-01", catalog) == "new-york-city"
    assert parse_site_code("ocp4-new-york-01", catalog) is None


@pytest.mark.parametrize(
    "spec",
    [
        "tel aviv",  # a space cannot appear in a hostname token
        "tlv_x",  # underscore is a separator, so it can never match
        "-tlv",  # leading separator
        "tlv-",  # trailing separator
        "tlv--x",  # doubled separator
    ],
)
def test_a_code_that_could_never_match_a_hostname_is_rejected_loudly(spec: str) -> None:
    """Silently accepting one would produce a site that exists in the UI
    and that no server can ever be assigned to.
    """
    with pytest.raises(SiteConfigurationError):
        SiteCatalog.from_spec(spec)


def test_a_code_written_in_uppercase_is_normalised_rather_than_rejected() -> None:
    """Hostname tokens are matched lowercase, so `TLV` in configuration
    is an operator being tidy, not an error worth failing startup over.
    """
    catalog = SiteCatalog.from_spec("TLV:Tel Aviv")

    assert catalog.codes == ("tlv",)
    assert parse_site_code("ocp4-prod-TLV-infra-01", catalog) == "tlv"


def test_a_duplicate_code_is_rejected() -> None:
    """Two entries for one code means one display name silently wins."""
    with pytest.raises(SiteConfigurationError, match="listed twice"):
        SiteCatalog.from_spec("tlv:Tel Aviv,tlv:Telaviv")


def test_a_spec_of_only_separators_is_rejected() -> None:
    """Distinct from empty, which legitimately means "use the default"."""
    with pytest.raises(SiteConfigurationError, match="lists no sites"):
        SiteCatalog.from_spec(",,,")


def test_the_regex_alternation_covers_every_configured_site() -> None:
    """The seeded classification rules interpolate this, so a site the
    alternation misses is a site whose servers never classify.
    """
    catalog = SiteCatalog.from_spec("lon:London,bat-yam:Bat Yam")

    assert catalog.alternation() == "lon|bat\\-yam"


def test_membership_is_case_insensitive_but_codes_stay_canonical() -> None:
    """A filter arriving from a URL should not miss on casing alone."""
    assert "TLV" in SITES
    assert "tlv" in SITES
    assert "nope" not in SITES
    assert SITES.codes == tuple(c.lower() for c in SITES.codes)


def test_an_unknown_code_still_renders_rather_than_disappearing() -> None:
    """A server stored under a site that has since been reconfigured away
    must still be displayable — that is the reason `Server.site_id` is a
    plain string and not an enum.
    """
    assert SITES.name_for("bat-yam") == "Bat Yam"
    assert SITES.name_for("decommissioned-dc") == "Decommissioned Dc"
