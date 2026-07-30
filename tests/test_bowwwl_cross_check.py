"""
Tests for src/bowwwl_cross_check/app.py, run against
tests/fixtures/bowwwl_fury_emerald_black_hybrid.html (current, symmetric
core, no discontinued/no MB Diff) and bowwwl_defender.html (retired,
asymmetric core, real MB Diff + PBA Approval Date + Discontinued marker)
-- both reconstructions using real field values/markup captured from live
bowwwl.com pages this session (see each fixture's header comment for
exactly what's real vs. inferred). Manual-runner pattern, run standalone
via `python3 tests/test_bowwwl_cross_check.py`.
"""
import datetime
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "bowwwl_cross_check"))

import app  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
FURY_URL = "https://www.bowwwl.com/bowling-ball-database/brunswick/fury-emeraldblack-hybrid"
DEFENDER_URL = "https://www.bowwwl.com/bowling-ball-database/brunswick/defender"


def _parsed_fury():
    html = (FIXTURES / "bowwwl_fury_emerald_black_hybrid.html").read_text()
    return app.parse_ball_page(html, FURY_URL)


def _parsed_defender():
    html = (FIXTURES / "bowwwl_defender.html").read_text()
    return app.parse_ball_page(html, DEFENDER_URL)


# --- Slug building ---

def test_slugify_bowwwl_ball_drops_slash_real_example():
    assert app.slugify_bowwwl_ball("Fury Emerald/Black Hybrid") == "fury-emeraldblack-hybrid"


def test_slugify_bowwwl_ball_simple_name():
    assert app.slugify_bowwwl_ball("Defender") == "defender"
    assert app.slugify_bowwwl_ball("Jackal Onyx") == "jackal-onyx"


def test_slugify_bowwwl_brand_multiword_real_examples():
    assert app.slugify_bowwwl_brand("900 Global") == "900-global"
    assert app.slugify_bowwwl_brand("Columbia 300") == "columbia-300"
    assert app.slugify_bowwwl_brand("Brunswick") == "brunswick"


def test_build_bowwwl_url():
    url = app.build_bowwwl_url("Brunswick", "Fury Emerald/Black Hybrid")
    assert url == FURY_URL


# --- Fury (current, symmetric) ---

def test_fury_basic_fields():
    p = _parsed_fury()
    assert p["name"] == "Fury Emerald/Black Hybrid"
    assert p["discontinued"] is False
    assert p["release_date"] == datetime.date(2026, 7, 16)
    assert p["pba_approval_date"] is None  # real: this field isn't present on Fury's page at all
    assert p["factory_finish"] == "500/1000/2000 Siaair Micro Pad"


def test_fury_coverstock_and_core():
    p = _parsed_fury()
    assert p["coverstock_name"] == "PK-26 Hybrid Coverstock"
    assert p["coverstock_type_raw"] == "Hybrid Reactive"
    assert p["core_name"] == "Fury Core"
    assert p["core_type_raw"] == "Symmetric"


def test_fury_five_skus_no_mass_bias():
    p = _parsed_fury()
    assert len(p["skus"]) == 5
    sku16 = next(s for s in p["skus"] if s["weight_lbs"] == 16)
    assert sku16["rg"] == 2.533
    assert sku16["differential"] == 0.033
    assert sku16["mass_bias"] is None  # symmetric -- no MB Diff card on any weight

    sku12 = next(s for s in p["skus"] if s["weight_lbs"] == 12)
    assert sku12["rg"] == 2.582
    assert sku12["differential"] == 0.038


# --- Defender (retired, asymmetric) ---

def test_defender_basic_fields():
    p = _parsed_defender()
    assert p["name"] == "Defender"
    assert p["discontinued"] is True
    assert p["release_date"] == datetime.date(2022, 3, 25)
    assert p["pba_approval_date"] == datetime.date(2022, 2, 20)


def test_defender_coverstock_and_core():
    p = _parsed_defender()
    assert p["coverstock_name"] == "ACT 3.0 Solid Coverstock"
    assert p["coverstock_type_raw"] == "Solid Reactive"
    assert p["core_name"] == "Portal X Core"
    assert p["core_type_raw"] == "Asymmetric"


def test_defender_five_skus_with_mass_bias():
    p = _parsed_defender()
    assert len(p["skus"]) == 5
    sku16 = next(s for s in p["skus"] if s["weight_lbs"] == 16)
    assert sku16["rg"] == 2.489
    assert sku16["differential"] == 0.046
    assert sku16["mass_bias"] == 0.013

    sku12 = next(s for s in p["skus"] if s["weight_lbs"] == 12)
    assert sku12["mass_bias"] == 0.014


# --- is_plausible_match ---

def test_is_plausible_match_true_for_shared_words():
    p = _parsed_fury()
    assert app.is_plausible_match(p, "Fury Emerald/Black Hybrid") is True
    assert app.is_plausible_match(p, "Fury Emerald Black") is True  # loose word-overlap match


def test_is_plausible_match_false_for_unrelated_name():
    p = _parsed_fury()
    assert app.is_plausible_match(p, "Storm Phaze II") is False


def test_is_plausible_match_false_when_no_h1_found():
    assert app.is_plausible_match({"name": None}, "Anything") is False


# --- compare_to_our_data ---

def test_compare_flags_rg_mismatch_beyond_tolerance():
    p = _parsed_fury()
    our_skus = [{"weight_lbs": 16, "rg": 2.600, "differential": 0.033, "mass_bias": None}]  # real bowwwl value is 2.533
    mismatches = app.compare_to_our_data(p, None, our_skus)
    rg_mismatch = next(m for m in mismatches if m["field_name"] == "rg_16lb")
    assert rg_mismatch["current_value"] == "2.6"
    assert rg_mismatch["proposed_value"] == "2.533"


def test_compare_no_mismatch_within_tolerance():
    p = _parsed_fury()
    our_skus = [{"weight_lbs": 16, "rg": 2.5335, "differential": 0.033, "mass_bias": None}]
    mismatches = app.compare_to_our_data(p, None, our_skus, tolerance=0.001)
    assert mismatches == []


def test_compare_skips_weight_we_dont_have():
    p = _parsed_fury()
    our_skus = [{"weight_lbs": 99, "rg": 1.0, "differential": 1.0, "mass_bias": None}]
    mismatches = app.compare_to_our_data(p, None, our_skus)
    assert mismatches == []  # no overlapping weight -- a coverage gap, not a value mismatch


def test_compare_flags_release_date_mismatch():
    p = _parsed_fury()
    mismatches = app.compare_to_our_data(p, datetime.date(2026, 1, 1), [])
    assert any(m["field_name"] == "release_date" for m in mismatches)


def test_compare_no_release_date_mismatch_when_equal():
    p = _parsed_fury()
    mismatches = app.compare_to_our_data(p, datetime.date(2026, 7, 16), [])
    assert mismatches == []


def test_compare_mass_bias_mismatch_on_asymmetric():
    p = _parsed_defender()
    our_skus = [{"weight_lbs": 16, "rg": 2.489, "differential": 0.046, "mass_bias": 0.020}]  # real bowwwl value is 0.013
    mismatches = app.compare_to_our_data(p, None, our_skus)
    mb_mismatch = next(m for m in mismatches if m["field_name"] == "mass_bias_16lb")
    assert mb_mismatch["proposed_value"] == "0.013"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS: {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")
