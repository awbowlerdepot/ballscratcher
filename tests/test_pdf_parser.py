"""
Tests for src/pdf_parser/app.py, run against real extracted PDF text --
tests/fixtures/crown_78u_info_sheet.txt and defender_info_sheet.txt were
captured verbatim via mcp__workspace__web_fetch against Brunswick's actual
CDN-hosted Info Sheet PDFs during this session (not reconstructed, unlike
the HTML fixtures -- see their own header notes in the architecture doc
for that distinction). Both fixture files include the raw performance-index
chart noise text that trails the real fields on the actual PDF page, so
these tests also confirm the parser correctly ignores that noise.
"""
import os
import sys

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


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS: {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")
