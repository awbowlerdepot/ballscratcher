"""
Tests for the HTML product scraper, run against two fixtures built from real
field values captured from brunswickbowling.com during architecture research
(see the comment blocks in tests/fixtures/crown_78u.html and defender.html
for exactly what's real vs. reconstructed, and why).

The two fixtures deliberately cover the two real patterns found during
research: a current ball with a full per-weight Core Numbers table and no
inline mass bias (Crown 78U), and a retired ball with only a single-value
spec row but with inline mass bias, and missing a Release Date field
(Defender). Testing against only one of these would miss real structural
variation this scraper has to handle.

Run with: pytest tests/test_product_scraper.py -v
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src" / "product_scraper"))

from app import (  # noqa: E402
    parse_product_page,
    parse_coverstock,
    parse_weights_available,
    parse_release_date,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def crown_78u():
    html = (FIXTURES / "crown_78u.html").read_text()
    return parse_product_page(html, "https://brunswickbowling.com/products/balls/current/crown-78u")


@pytest.fixture
def defender():
    html = (FIXTURES / "defender.html").read_text()
    return parse_product_page(html, "https://brunswickbowling.com/products/balls/retired/defender")


# --- Crown 78U: current ball, full per-weight table, no inline mass bias ---

def test_crown_78u_basic_fields(crown_78u):
    assert crown_78u["name"] == "Crown 78U"
    assert crown_78u["status"] == "current"
    assert crown_78u["color"] == "Purple / Grey"
    assert crown_78u["core_name"] == "Tiered Hexagon"
    assert crown_78u["part_number"] == "60-108363-93X"
    assert crown_78u["factory_finish"] == "500, 1000 Siaair Micro Pad"


def test_crown_78u_coverstock_split(crown_78u):
    # Real "Cover Type" value is bare "Urethane" -- no solid/pearl/hybrid
    # given, so coverstock_type should be None, not a guess.
    assert crown_78u["coverstock_material"] == "urethane"
    assert crown_78u["coverstock_type"] is None
    assert crown_78u["coverstock_name"] == "Urethane Solid 78D"


def test_crown_78u_weights_range(crown_78u):
    assert crown_78u["weights_available"] == (12, 16)


def test_crown_78u_full_weight_breakdown(crown_78u):
    skus = {s["weight_lbs"]: s for s in crown_78u["skus"]}
    assert set(skus.keys()) == {16, 15, 14, 13, 12}
    assert skus[16]["rg"] == 2.577
    assert skus[16]["differential"] == 0.039
    assert skus[12]["rg"] == 2.597
    assert skus[12]["differential"] == 0.040
    # No mass bias anywhere on this page -- real finding from research,
    # not a gap in the fixture.
    assert all(s["mass_bias"] is None for s in crown_78u["skus"])


def test_crown_78u_resources(crown_78u):
    assert crown_78u["resources"]["info_sheet_url"].endswith("Crown_78U_Info_Sheet_1025-12.pdf")
    assert crown_78u["resources"]["ball_talker_url"].endswith("Crown_78U_Ball_Talker_1025-11.pdf")


def test_crown_78u_images(crown_78u):
    image_types = [img["image_type"] for img in crown_78u["images"]]
    assert image_types[0] == "main"
    callouts = [img for img in crown_78u["images"] if img["image_type"] == "core_callout"]
    assert len(callouts) == 2
    # filename-based weight range parsing, not the inconsistent spelled-out alt text
    assert {(c["weight_lbs_context_low"], c["weight_lbs_context_high"]) for c in callouts} == {(14, 16), (12, 13)}


# --- Defender: retired ball, single-value spec row, inline mass bias ---

def test_defender_basic_fields(defender):
    assert defender["name"] == "Defender"
    assert defender["status"] == "retired"
    assert defender["core_name"] == "Portal X"


def test_defender_coverstock_split(defender):
    assert defender["coverstock_material"] == "reactive_resin"
    assert defender["coverstock_type"] == "solid"


def test_defender_single_value_defaults_to_15lb(defender):
    """The architecture review's key convention: a single RG/DIFF/mass-bias
    value with no per-weight breakdown is the 15 lb ball."""
    assert len(defender["skus"]) == 1
    sku = defender["skus"][0]
    assert sku["weight_lbs"] == 15
    assert sku["rg"] == 2.473
    assert sku["differential"] == 0.054


def test_defender_inline_mass_bias_captured(defender):
    """Confirms the parser captures ASY when it IS inline in HTML (real,
    observed on this specific retired page), even though Crown 78U's page
    has no mass bias in HTML at all -- both are real, both must work."""
    sku = defender["skus"][0]
    assert sku["mass_bias"] == 0.015


def test_defender_missing_release_date_does_not_break_parsing(defender):
    """Defender's real spec table has no Release Date row -- confirms the
    scraper doesn't require every known field to be present."""
    assert "release_date_raw" in defender
    assert defender["release_date_raw"] is None
    assert defender["release_date"] is None


def test_parse_release_date_real_values_from_architecture_doc():
    """Real values recorded in brunswick-scraper-architecture-review.md
    during live research (Crown Victory = April 2025, Crown 78U =
    December 2025) -- not present in either fixture's own spec table
    (crown_78u.html's reconstruction predates this field being tracked),
    so tested directly against the pure function rather than through a
    fixture."""
    import datetime
    assert parse_release_date("April 2025") == datetime.date(2025, 4, 1)
    assert parse_release_date("December 2025") == datetime.date(2025, 12, 1)


def test_parse_release_date_returns_none_for_unparseable():
    assert parse_release_date("") is None
    assert parse_release_date(None) is None
    assert parse_release_date("Q1 2025") is None


# --- Helper functions tested directly, beyond what the fixtures exercise ---

def test_parse_coverstock_hybrid_reactive():
    assert parse_coverstock("Hybrid Reactive") == {
        "coverstock_material": "reactive_resin",
        "coverstock_type": "hybrid",
    }


def test_parse_coverstock_bare_polyester_no_type():
    """Real pattern from Hammer's Black Widow Viz-A-Ball: Cover Type is just
    "Polyester", no Solid/Pearl given -- graphic balls often don't disclose
    a type, per the architecture doc. Should stay None, not guess."""
    assert parse_coverstock("Polyester") == {
        "coverstock_material": "polyester_plastic",
        "coverstock_type": None,
    }


def test_parse_weights_available_handles_period_and_lbs_suffix():
    assert parse_weights_available("16-12 lbs.") == (12, 16)


def test_parse_weights_available_returns_none_for_unexpected_format():
    assert parse_weights_available("assorted") is None
