"""
Tests for src/pdf_parser/app.py, run against real extracted PDF text --
tests/fixtures/crown_78u_info_sheet.txt and defender_info_sheet.txt were
captured verbatim via mcp__workspace__web_fetch against Brunswick's actual
CDN-hosted Info Sheet PDFs during this session (not reconstructed, unlike
the HTML fixtures -- see their own header notes in the architecture doc
for that distinction). Both fixture files include the raw performance-index
chart noise text that trails the real fields on the actual PDF page, so
these tests also confirm the parser correctly ignores that noise.

tests/fixtures/mastermind_strategy_info_sheet.txt -- captured the same way
(mcp__workspace__web_fetch against Brunswick's real CDN URL), in a later
session, for a real, live-reported parsing bug: Mastermind Strategy (a
retired asymmetric-core ball) uses an older Info Sheet layout entirely,
with a 5-row RG MAX/INT/Min/Diff/ASY breakdown instead of the modern
bare RG/DIFF/ASY lines, and ALL-CAPS field labels instead of title case.
See parse_weight_table's and parse_fields' docstrings for exactly how
each broke before this fixture's tests were added.
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "pdf_parser"))

import app  # noqa: E402

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES_DIR, name)) as f:
        return f.read()


def test_crown_78u_fields():
    parsed = app.parse_info_sheet(_load("crown_78u_info_sheet.txt"))
    assert parsed["part_number"] == "60-108363-93X"
    assert parsed["color"] == "Purple / Grey"
    assert parsed["core_name"] == "Tiered Hexagon"
    assert parsed["coverstock_name"] == "Urethane Solid 78D"
    assert parsed["cover_type"] == "Urethane"
    assert parsed["factory_finish"] == "500, 1000 Siaair Micro Pad"
    assert parsed["warranty"] == "Two years from purchase date"


def test_crown_78u_skus_no_mass_bias():
    """Tiered Hexagon is a symmetric core -- the real PDF has no ASY row at
    all, unlike Defender's. Confirms the parser doesn't invent a value or
    choke on the missing row."""
    parsed = app.parse_info_sheet(_load("crown_78u_info_sheet.txt"))
    skus = parsed["skus"]
    assert [s["weight_lbs"] for s in skus] == [16, 15, 14, 13, 12]
    assert all(s["mass_bias"] is None for s in skus)

    by_weight = {s["weight_lbs"]: s for s in skus}
    # Real PDF value -- deliberately differs from the HTML fixture's 2.577
    # at 16 lb, see test_find_mismatches_detects_real_crown_78u_discrepancy.
    assert by_weight[16]["rg"] == 2.557
    assert by_weight[16]["differential"] == 0.039
    assert by_weight[12]["rg"] == 2.597
    assert by_weight[12]["differential"] == 0.040


def test_defender_fields():
    parsed = app.parse_info_sheet(_load("defender_info_sheet.txt"))
    assert parsed["part_number"] == "60-106724-93X"
    assert parsed["core_name"] == "Portal X"
    assert parsed["coverstock_name"] == "A.C.T. 3.0 Solid"
    assert parsed["cover_type"] == "Solid Reactive"
    assert parsed["factory_finish"] == "500, 2000 Siaair Micro Pad"


def test_defender_skus_full_breakdown_with_mass_bias():
    """The real motivating case: Defender's HTML page only ever exposed a
    single 15 lb reference value, but its PDF has the complete 5-weight
    breakdown including ASY (mass bias) -- which HTML never carries for
    this ball at all."""
    parsed = app.parse_info_sheet(_load("defender_info_sheet.txt"))
    skus = parsed["skus"]
    assert [s["weight_lbs"] for s in skus] == [16, 15, 14, 13, 12]
    assert all(s["mass_bias"] is not None for s in skus)

    by_weight = {s["weight_lbs"]: s for s in skus}
    # This is the real value HTML's single-value spec row showed too --
    # exact match validates both the 15 lb-default convention and that
    # HTML/PDF draw from consistent underlying data for this ball.
    assert by_weight[15]["rg"] == 2.473
    assert by_weight[15]["differential"] == 0.054
    assert by_weight[15]["mass_bias"] == 0.015


def test_mastermind_strategy_fields_all_caps_labels():
    """Real bug: this older sheet's field labels are ALL CAPS ("PART
    NUMBER", "COLOR", ...) where modern sheets use title case ("Part
    Number", "Color"). Before parse_fields' case-insensitive fix, every
    one of these came back None."""
    parsed = app.parse_info_sheet(_load("mastermind_strategy_info_sheet.txt"))
    assert parsed["part_number"] == "60105839"
    assert parsed["color"] == "Blue / Violet / Orange"
    assert parsed["core_name"] == "Modified Mastermind Asymmetric"
    assert parsed["coverstock_name"] == "Relativity Solid"
    assert parsed["factory_finish"] == "500 / 2000 Siaair Micro Pad"
    # This older layout has no separate "Cover Type"/"Warranty" lines at
    # all -- correctly None, not a parsing failure.
    assert parsed["cover_type"] is None
    assert parsed["warranty"] is None


def test_mastermind_strategy_skus_older_rg_max_int_min_layout():
    """The real motivating case: this sheet's weight table has five rows
    (RG MAX/INT/Min/Diff/ASY) instead of the modern two/three (RG/DIFF/
    ASY), because it exposes the raw measurements the modern sheets
    collapse away. "RG Min" is what maps to this project's "rg" column --
    confirmed against the values below, which are this exact ball's own
    published spec-table numbers (RG 2.504 / DIFF 0.048 / ASY 0.013, all
    at 15 lb)."""
    parsed = app.parse_info_sheet(_load("mastermind_strategy_info_sheet.txt"))
    skus = parsed["skus"]
    assert [s["weight_lbs"] for s in skus] == [16, 15, 14, 13, 12]

    by_weight = {s["weight_lbs"]: s for s in skus}
    assert by_weight[15]["rg"] == 2.504
    assert by_weight[15]["differential"] == 0.048
    assert by_weight[15]["mass_bias"] == 0.013
    # Spot-check a second weight too, not just the one the spec table
    # happens to publish -- confirms the whole row, not a coincidence at
    # 15 lb only.
    assert by_weight[16]["rg"] == 2.492
    assert by_weight[16]["differential"] == 0.048
    assert by_weight[16]["mass_bias"] == 0.013
    assert by_weight[12]["rg"] == 2.612
    assert by_weight[12]["differential"] == 0.043
    assert by_weight[12]["mass_bias"] == 0.011


def test_mastermind_strategy_ignores_headline_only_rg_differential_line():
    """This sheet also has a standalone "RG DIFFERENTIAL .048" line (a
    single headline value, not a per-weight row) that starts with "RG "
    just like the real per-weight rows do. Must not leak into any
    result -- confirmed above that differential values come from the
    real "RG Diff" per-weight row instead, which this test just
    reinforces isn't a coincidental match."""
    parsed = app.parse_info_sheet(_load("mastermind_strategy_info_sheet.txt"))
    diffs = {s["weight_lbs"]: s["differential"] for s in parsed["skus"]}
    assert diffs == {16: 0.048, 15: 0.048, 14: 0.048, 13: 0.043, 12: 0.043}


def test_find_mismatches_detects_real_crown_78u_discrepancy():
    """The real, verified data point that motivated this function: Crown
    78U's HTML page says 16 lb RG is 2.577; its own PDF says 2.557."""
    html_skus = [{"weight_lbs": 16, "rg": 2.577, "differential": 0.039, "mass_bias": None}]
    pdf_skus = app.parse_info_sheet(_load("crown_78u_info_sheet.txt"))["skus"]

    mismatches = app.find_mismatches(html_skus, pdf_skus)

    assert len(mismatches) == 1
    assert mismatches[0]["weight_lbs"] == 16
    assert mismatches[0]["field_name"] == "rg"
    assert mismatches[0]["html_value"] == 2.577
    assert mismatches[0]["pdf_value"] == 2.557


def test_find_mismatches_no_false_positive_when_html_lacks_mass_bias():
    """Defender's HTML never had a mass_bias value at all (None). That's the
    PDF filling a gap, not a disagreement -- must not be flagged."""
    html_skus = [{"weight_lbs": 15, "rg": 2.473, "differential": 0.054, "mass_bias": None}]
    pdf_skus = app.parse_info_sheet(_load("defender_info_sheet.txt"))["skus"]

    mismatches = app.find_mismatches(html_skus, pdf_skus)

    assert mismatches == []


def test_find_mismatches_ignores_weights_html_never_had():
    """Crown 78U HTML (per the real product page) only ever had a 16 lb row.
    The PDF's 15/14/13/12 lb rows aren't mismatches -- there's nothing on
    the HTML side to disagree with."""
    html_skus = [{"weight_lbs": 16, "rg": 2.557, "differential": 0.039, "mass_bias": None}]
    pdf_skus = app.parse_info_sheet(_load("crown_78u_info_sheet.txt"))["skus"]

    mismatches = app.find_mismatches(html_skus, pdf_skus)

    assert mismatches == []


def test_find_mismatches_handles_decimal_from_postgres():
    """Real production bug (found via live CloudWatch logs on first-ever real
    deploy, not a hypothetical): existing_by_weight is built from a Postgres
    'numeric' column, which psycopg2 returns as decimal.Decimal, not float.
    pdf_skus values come from _to_float() and are plain float. abs(Decimal -
    float) raises TypeError. Every prior test here used plain floats on both
    sides, which is exactly why this shipped -- this test uses Decimal on the
    HTML side to match what the real database actually returns."""
    html_skus = [{
        "weight_lbs": 16,
        "rg": Decimal("2.577"),
        "differential": Decimal("0.039"),
        "mass_bias": None,
    }]
    pdf_skus = app.parse_info_sheet(_load("crown_78u_info_sheet.txt"))["skus"]

    mismatches = app.find_mismatches(html_skus, pdf_skus)

    assert len(mismatches) == 1
    assert mismatches[0]["weight_lbs"] == 16
    assert mismatches[0]["field_name"] == "rg"


def test_find_mismatches_decimal_exact_match_not_flagged():
    """Same Decimal-vs-float shape as above, but values agree within
    tolerance -- must not be flagged just because the types differ."""
    html_skus = [{
        "weight_lbs": 16,
        "rg": Decimal("2.557"),
        "differential": Decimal("0.039"),
        "mass_bias": None,
    }]
    pdf_skus = app.parse_info_sheet(_load("crown_78u_info_sheet.txt"))["skus"]

    mismatches = app.find_mismatches(html_skus, pdf_skus)

    assert mismatches == []


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS: {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")
