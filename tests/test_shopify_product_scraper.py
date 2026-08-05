"""
Tests for src/shopify_product_scraper/app.py's pure parsing functions, run
against four real fixtures spanning three eras of Hammer's body_html
markup -- see that module's docstring for exactly what real, confirmed
formatting differences each fixture exercises:

- hammer_black_widow_3_0_dynasty.json: modern, asymmetric (has ASY),
  "CORE"/<span> with no separating whitespace, "COVER TYPE" value split
  across a <span> plus trailing plain text.
- hammer_fallout.json: modern, symmetric (no ASY at all, no CORE TYPE
  field either -- core_type must be inferred), "COVERSTOCK" label spread
  across <strong> and plain text before the <span>.
- hammer_3d_offset.json: older retired, explicit "CORE TYPE: Asymmetric"
  field, "RG / DIFF / ASY" heading instead of "RG / DIFF".
- hammer_absolut_curve.json: very old (2018) retired, "#10 RG (2.72) Diff
  (.031)" weight-list format (no "lb", lowercase "Diff", no ASY),
  "FACTORY FINISH"/"BEST LANE CONDITION"/"AVAILABLE WEIGHTS" label
  spellings, no RELEASE DATE field at all.
- hammer_scandal.json: real captured body_html from
  https://hammerbowling.com/products/scandal.json (fetched live to
  diagnose a real production bug -- see below), every BALL SPECS <li>
  pretty-printed with a newline between <li> and its <strong> label
  (e.g. "<li>\\n<strong>CORE</strong><span> </span>Scandal</li>").

Regression coverage for a real production bug (all Hammer fixtures above
were hand-reconstructed and didn't reproduce it -- hammer_scandal.json's
body_html is the real fetched bytes): parse_ball_specs() originally sliced
each <li>'s full get_text() by len(strong.get_text()) to find where the
label ended and the value began. That works when there's no whitespace
between <li> and <strong>, but real Hammer markup has a newline there,
which li.get_text() includes and strong.get_text() doesn't -- so the
slice started one character early, landing on the label's own last
character. Every product's BALL_SPEC_LABEL_MAP field shipped to
production with this corruption (caught via cores.name, e.g. "Scandal"
became "E Scandal" for every one of 219 Hammer products) before being
fixed to walk strong.next_siblings instead of slicing by character
count -- see parse_ball_specs's own docstring for the full story.

Manual-runner pattern, run standalone via
`python3 tests/test_shopify_product_scraper.py`, matching every other
scraper's test file in this project (real pytest isn't installable in
this sandbox).
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "shopify_product_scraper"))

import app  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text())["product"]


def _parsed(fixture_name, url):
    return app.parse_product_page(_load(fixture_name), url)


BWD_URL = "https://hammerbowling.com/products/black-widow-3-0-dynasty"
FALLOUT_URL = "https://hammerbowling.com/products/fallout"
OFFSET_URL = "https://hammerbowling.com/products/3-d-offset"
CURVE_URL = "https://hammerbowling.com/products/absolut-curve"
SCANDAL_URL = "https://hammerbowling.com/products/scandal"


# --- Black Widow 3.0 Dynasty: modern, asymmetric ---

def test_bwd_basic_fields():
    p = _parsed("hammer_black_widow_3_0_dynasty.json", BWD_URL)
    assert p["name"] == "Black Widow 3.0 Dynasty"
    assert p["color"] == "Ultraviolet / Black"
    assert p["part_number"] == "60-108557-93X"
    assert p["performance_level_raw"] == "Upper Mid"


def test_bwd_core_name_parses_despite_no_whitespace_between_tags():
    """Real, confirmed case: <strong>CORE</strong><span>Gas Mask</span>
    with no space anywhere between them -- see module docstring."""
    p = _parsed("hammer_black_widow_3_0_dynasty.json", BWD_URL)
    assert p["core_name"] == "Gas Mask"


def test_bwd_coverstock_split_across_span_and_trailing_text():
    """Real, confirmed case: COVER TYPE's value is <span>Solid</span>
    Reactive -- "Reactive" is plain text outside the span."""
    p = _parsed("hammer_black_widow_3_0_dynasty.json", BWD_URL)
    assert p["coverstock_name"] == "HK22 - Cohesion Solid"
    assert p["coverstock_material"] == "reactive_resin"
    assert p["coverstock_type"] == "solid"


def test_bwd_skus_include_asy_and_core_type_is_asymmetric():
    p = _parsed("hammer_black_widow_3_0_dynasty.json", BWD_URL)
    assert p["core_type"] == "asymmetric"
    assert len(p["skus"]) == 5
    sixteen = next(s for s in p["skus"] if s["weight_lbs"] == 16)
    assert sixteen == {"weight_lbs": 16, "rg": 2.510, "differential": 0.048, "mass_bias": 0.015}
    twelve = next(s for s in p["skus"] if s["weight_lbs"] == 12)
    assert twelve == {"weight_lbs": 12, "rg": 2.612, "differential": 0.043, "mass_bias": 0.011}


def test_bwd_weights_available_and_release_date():
    p = _parsed("hammer_black_widow_3_0_dynasty.json", BWD_URL)
    assert p["weights_available"] == (12, 16)
    assert str(p["release_date"]) == "2026-01-15"


def test_bwd_description_is_first_substantial_paragraph():
    p = _parsed("hammer_black_widow_3_0_dynasty.json", BWD_URL)
    assert p["description"].startswith("The Black Widow 3.0 Dynasty was created to enhance the line")


def test_bwd_images_classified_by_alt_keyword():
    p = _parsed("hammer_black_widow_3_0_dynasty.json", BWD_URL)
    assert len(p["images"]) == 4
    assert p["images"][0]["image_type"] == "main"
    assert p["images"][1]["image_type"] == "core_callout"
    assert p["images"][2]["image_type"] == "core_callout"
    assert p["images"][3]["image_type"] == "performance_badge"


# --- Fallout: modern, symmetric (no ASY, no explicit CORE TYPE) ---

def test_fallout_core_type_inferred_symmetric_when_no_asy():
    p = _parsed("hammer_fallout.json", FALLOUT_URL)
    assert len(p["skus"]) == 5
    assert all(s["mass_bias"] is None for s in p["skus"])
    assert p["core_type"] == "symmetric"


def test_fallout_coverstock_label_split_across_strong_and_span():
    """Real, confirmed case: <strong>COVERSTOCK </strong>HK22C²
    <span>Solid</span> -- part of the value sits between the </strong> and
    the <span> as plain text."""
    p = _parsed("hammer_fallout.json", FALLOUT_URL)
    assert p["coverstock_name"] == "HK22C² Solid"


def test_fallout_main_image_with_no_alt_still_classified_main_by_position():
    p = _parsed("hammer_fallout.json", FALLOUT_URL)
    assert p["images"][0]["image_type"] == "main"


# --- 3-D Offset: older retired, explicit CORE TYPE field ---

def test_3d_offset_explicit_core_type_wins_over_inference():
    p = _parsed("hammer_3d_offset.json", OFFSET_URL)
    assert p["core_type"] == "asymmetric"
    assert p["core_name"] == "High Rev Offset"


def test_3d_offset_rg_diff_asy_heading_variant_still_parses():
    """Heading is "RG / DIFF / ASY", not "RG / DIFF" -- find_section()
    matches on the "RG" prefix so both variants are found the same way."""
    p = _parsed("hammer_3d_offset.json", OFFSET_URL)
    assert len(p["skus"]) == 5
    sixteen = next(s for s in p["skus"] if s["weight_lbs"] == 16)
    assert sixteen == {"weight_lbs": 16, "rg": 2.501, "differential": 0.046, "mass_bias": 0.007}


def test_3d_offset_release_date_and_pearl_coverstock():
    p = _parsed("hammer_3d_offset.json", OFFSET_URL)
    assert str(p["release_date"]) == "2022-02-25"
    assert p["coverstock_type"] == "pearl"


def test_3d_offset_images_without_alt_fall_back_to_other():
    """Real, confirmed case: this retired listing's core/badge images
    carry no alt text at all in the collection JSON -- classify_image
    can't keyword-match, so anything past position 1 falls back to
    "other" rather than guessing."""
    p = _parsed("hammer_3d_offset.json", OFFSET_URL)
    assert p["images"][0]["image_type"] == "main"
    assert p["images"][1]["image_type"] == "other"
    assert p["images"][2]["image_type"] == "other"
    assert p["images"][3]["image_type"] == "other"


# --- Absolut Curve: very old (2018), different weight-list format entirely ---

def test_absolut_curve_old_weight_list_format():
    """"#10 RG (2.72) Diff (.031)" -- no "lb" suffix, lowercase "Diff",
    leading "#" before the weight number, no ASY at all."""
    p = _parsed("hammer_absolut_curve.json", CURVE_URL)
    assert len(p["skus"]) == 7
    ten = next(s for s in p["skus"] if s["weight_lbs"] == 10)
    assert ten == {"weight_lbs": 10, "rg": 2.72, "differential": 0.031, "mass_bias": None}
    sixteen = next(s for s in p["skus"] if s["weight_lbs"] == 16)
    assert sixteen == {"weight_lbs": 16, "rg": 2.50, "differential": 0.042, "mass_bias": None}


def test_absolut_curve_old_label_spellings():
    """"FACTORY FINISH"/"BEST LANE CONDITION"/"AVAILABLE WEIGHTS" instead
    of the modern "FINISH"/"LANE CONDITION"/"WEIGHTS" -- BALL_SPEC_LABEL_MAP
    maps both spellings to the same canonical keys."""
    p = _parsed("hammer_absolut_curve.json", CURVE_URL)
    assert p["factory_finish"] == "500-500-500 Abralon® -1500 Abranet™- Powerhouse™ Factory Finish Polish"
    assert p["weights_available"] == (10, 16)


def test_absolut_curve_no_release_date_field_returns_none():
    p = _parsed("hammer_absolut_curve.json", CURVE_URL)
    assert p["release_date"] is None
    assert p["release_date_raw"] is None


def test_absolut_curve_core_type_inferred_symmetric_no_core_type_field_no_asy():
    p = _parsed("hammer_absolut_curve.json", CURVE_URL)
    assert p["core_type"] == "symmetric"


def test_absolut_curve_description_from_plain_untitled_paragraphs():
    """No "p1" CSS class on this era's description paragraphs, and no
    marketing-tagline <h3> before them either -- confirms parse_description
    doesn't depend on either."""
    p = _parsed("hammer_absolut_curve.json", CURVE_URL)
    assert p["description"] == "Absolut Curve has everything that Hammer bowlers want. With the combination of the new FatMax core shape and our H-150 Crosscover Reactive shell, the Absolut Curve gives Hammer bowlers the most angular mid-price ball from the Hammer line in years."


# --- Scandal: real fetched body_html, regression coverage for the
# leading-newline-before-<strong> production bug (see module docstring) ---

def test_scandal_core_name_not_corrupted_by_leading_newline_before_strong():
    """Regression test for a real production bug: real Hammer markup has
    a newline between <li> and <strong> (confirmed live against
    hammerbowling.com/products/scandal.json), which desynced the old
    char-slice implementation and produced a leading "E " on every
    Hammer product's cores.name in production (219/219 affected)."""
    p = _parsed("hammer_scandal.json", SCANDAL_URL)
    assert p["core_name"] == "Scandal"
    assert p["core_type"] == "symmetric"


def test_scandal_color_not_corrupted_same_bug_class():
    """Same bug, a different field: COLOR's last letter is "R", so the
    old slicing bug would have produced "R Blue / Green / Black" here --
    confirms the fix isn't specific to the word "CORE"."""
    p = _parsed("hammer_scandal.json", SCANDAL_URL)
    assert p["color"] == "Blue / Green / Black"


def test_scandal_coverstock_and_weights_not_corrupted():
    p = _parsed("hammer_scandal.json", SCANDAL_URL)
    assert p["coverstock_name"] == "Semtex Solid CFI"
    assert p["factory_finish"] == "500 / 2000 Abralon®"
    assert p["weights_available"] == (12, 16)


def test_scandal_rg_diff_list_unaffected_no_asy():
    """parse_rg_diff_list was never affected by this bug -- it regex-
    searches the LI's full text rather than slicing by label length --
    included here to confirm the RG/DIFF section still parses correctly
    against this same real fixture."""
    p = _parsed("hammer_scandal.json", SCANDAL_URL)
    assert len(p["skus"]) == 5
    sixteen = next(s for s in p["skus"] if s["weight_lbs"] == 16)
    assert sixteen == {"weight_lbs": 16, "rg": 2.49, "differential": 0.047, "mass_bias": None}


def test_scandal_full_ball_specs_raw_dict_every_field_survives_leading_newline():
    """Exercises parse_ball_specs() directly (rather than just the subset
    of fields parse_product_page surfaces) to confirm every
    BALL_SPEC_LABEL_MAP field -- not just core_name/color -- survives the
    real leading-newline-before-<strong> markup."""
    product = _load("hammer_scandal.json")
    soup = app.BeautifulSoup(product["body_html"], "lxml")
    raw = app.parse_ball_specs(app._find_section(soup, "BALL SPECS"))
    assert raw == {
        "color": "Blue / Green / Black",
        "reaction": "Aggressive Overall Hook",
        "coverstock_name": "Semtex Solid CFI",
        "factory_finish": "500 / 2000 Abralon®",
        "lane_condition": "Medium to Heavy Oil",
        "core_name": "Scandal",
        "core_type_raw": "Symmetrical",
        "weights_raw": "12-16 lb",
    }


# --- Standalone unit coverage for the smaller pure helpers ---

def test_parse_weights_available_returns_none_for_empty():
    assert app.parse_weights_available(None) is None
    assert app.parse_weights_available("") is None


def test_parse_release_date_returns_none_for_unparseable():
    assert app.parse_release_date("sometime next year") is None


def test_parse_core_type_prefers_explicit_field_over_inference():
    assert app.parse_core_type("Symmetric", [{"mass_bias": 0.011}]) == "symmetric"


def test_classify_image_keyword_beats_position():
    """A "core" keyword in alt text wins even at position 1 -- confirms
    the classifier really is content-first, not position-first."""
    assert app.classify_image("A core callout image", position=1) == "core_callout"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS: {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")
