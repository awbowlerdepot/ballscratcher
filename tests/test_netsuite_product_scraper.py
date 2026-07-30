"""
Tests for src/netsuite_product_scraper/app.py, run against
tests/fixtures/motiv_sigma_tour_pearl.html (symmetric core) and
motiv_jackal_onyx.html (asymmetric core) -- both reconstructions using
real field values captured from live motivbowling.com product pages this
session (see each fixture's header comment for exactly what's real).
Manual-runner pattern, run standalone via
`python3 tests/test_netsuite_product_scraper.py`.
"""
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "netsuite_product_scraper"))

import app  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SIGMA_URL = "https://www.motivbowling.com/products/balls/medium-oil/sigma-tour-pearl.html"
JACKAL_URL = "https://www.motivbowling.com/products/balls/heavy-oil/jackal-onyx.html"


def _parsed_sigma(status="current"):
    html = (FIXTURES / "motiv_sigma_tour_pearl.html").read_text()
    return app.parse_product_page(html, SIGMA_URL, status=status)


def _parsed_jackal(status="current"):
    html = (FIXTURES / "motiv_jackal_onyx.html").read_text()
    return app.parse_product_page(html, JACKAL_URL, status=status)


# --- Sigma Tour Pearl (symmetric core) ---

def test_sigma_basic_fields():
    p = _parsed_sigma()
    assert p["name"] == "Sigma Tour Pearl"
    assert p["color"] is None  # no " - " colorway separator in this name
    assert p["part_number"] == "MTVBSAPRL"
    assert p["price_raw"] == "$224.99"
    assert p["release_date_raw"] == "AVAILABLE 7/8/2026"


def test_sigma_status_passed_through_from_job():
    """Status has no on-page signal for MOTIV -- it must come from
    whatever the caller passes in (set by netsuite_url_discovery in
    production)."""
    assert _parsed_sigma(status="current")["status"] == "current"
    assert _parsed_sigma(status="retired")["status"] == "retired"


def test_sigma_status_defaults_to_current_in_handler():
    job = {"url": SIGMA_URL, "brand_id": "brand-1"}  # no "status" key
    assert job.get("status", "current") == "current"


def test_sigma_specs():
    p = _parsed_sigma()
    assert p["core_name"] == "Sigma Symmetric"
    assert p["coverstock_name"] == "Atomic Propulsion Pearl Reactive"
    assert p["factory_finish"] == "5000 Grit LSS"
    assert p["weights_available"] == (12, 16)


def test_sigma_coverstock_material_and_type_from_single_field():
    p = _parsed_sigma()
    assert p["coverstock_material"] == "reactive_resin"
    assert p["coverstock_type"] == "pearl"


def test_sigma_five_skus_no_mass_bias():
    p = _parsed_sigma()
    assert len(p["skus"]) == 5
    weights = [s["weight_lbs"] for s in p["skus"]]
    assert weights == [12, 13, 14, 15, 16]

    sku16 = next(s for s in p["skus"] if s["weight_lbs"] == 16)
    assert sku16["rg"] == 2.46
    assert sku16["differential"] == 0.044
    assert sku16["mass_bias"] is None  # symmetric core -- no Int. Differential on this page

    sku12 = next(s for s in p["skus"] if s["weight_lbs"] == 12)
    assert sku12["rg"] == 2.63
    assert sku12["differential"] == 0.024


def test_sigma_downloads():
    p = _parsed_sigma()
    assert p["resources"]["sell_sheet_url"].endswith("userfiles/filemanager/c36cw2r06mwa9gjovr2p")
    assert p["resources"]["shelf_talker_url"].endswith("userfiles/filemanager/h9s22i72056smvvoxatj")


def test_sigma_images_main_plus_other_no_core_callout():
    """Sigma Tour Pearl's fixture has 3 main gallery photos and no
    core-image variant (only observed on Jackal Onyx this session)."""
    p = _parsed_sigma()
    assert len(p["images"]) == 3
    assert p["images"][0]["image_type"] == "main"
    assert p["images"][0]["source_url"].endswith("g7990y7nd5bkbm1xmic9")
    assert p["images"][1]["image_type"] == "other"
    assert p["images"][2]["image_type"] == "other"


def test_sigma_motion_metrics_captured_not_persisted():
    p = _parsed_sigma()
    assert p["motion_metrics_raw"]["length"] == "67"
    assert p["motion_metrics_raw"]["backend"] == "78"
    assert p["motion_metrics_raw"]["hook"] == "65"
    assert p["motion_metrics_raw"]["flare potential"] == '4"+'


# --- Jackal Onyx (asymmetric core -- covers Int. Differential / mass_bias) ---

def test_jackal_basic_fields():
    p = _parsed_jackal()
    assert p["name"] == "Jackal Onyx"
    assert p["part_number"] == "MTVBJKOYX"
    assert p["price_raw"] == "$239.99"


def test_jackal_coverstock_solid_not_pearl():
    p = _parsed_jackal()
    assert p["coverstock_material"] == "reactive_resin"
    assert p["coverstock_type"] == "solid"


def test_jackal_five_skus_with_mass_bias():
    p = _parsed_jackal()
    assert len(p["skus"]) == 5

    sku16 = next(s for s in p["skus"] if s["weight_lbs"] == 16)
    assert sku16["rg"] == 2.48
    assert sku16["differential"] == 0.047
    assert sku16["mass_bias"] == 0.013

    sku12 = next(s for s in p["skus"] if s["weight_lbs"] == 12)
    assert sku12["rg"] == 2.64
    assert sku12["differential"] == 0.030
    assert sku12["mass_bias"] == 0.010


def test_jackal_downloads_different_labels_than_sigma():
    p = _parsed_jackal()
    assert "shelf_talker_url" in p["resources"]
    assert "factory_finish_guide_url" in p["resources"]
    assert "sell_sheet_url" not in p["resources"]  # this ball doesn't have one


def test_jackal_images_includes_core_callout():
    p = _parsed_jackal()
    image_types = [img["image_type"] for img in p["images"]]
    assert "core_callout" in image_types
    core_img = next(img for img in p["images"] if img["image_type"] == "core_callout")
    assert "filemanager-format/core-image" in core_img["source_url"]


# --- Helper functions tested directly ---

def test_parse_name_and_colorway_splits_designer_series_name():
    base, colorway = app.parse_name_and_colorway("Ascend - Green/Teal/Black")
    assert base == "Ascend"
    assert colorway == "Green/Teal/Black"


def test_parse_name_and_colorway_no_separator_returns_none_colorway():
    base, colorway = app.parse_name_and_colorway("Sigma Tour Pearl")
    assert base == "Sigma Tour Pearl"
    assert colorway is None


def test_parse_weights_available_no_lb_suffix():
    assert app.parse_weights_available("12, 13, 14, 15, 16") == (12, 16)


def test_parse_weights_available_returns_none_for_empty():
    assert app.parse_weights_available("") is None
    assert app.parse_weights_available(None) is None


def test_parse_coverstock_returns_none_when_missing():
    result = app.parse_coverstock(None)
    assert result == {"coverstock_material": None, "coverstock_type": None}


def test_parse_release_date_strips_available_prefix():
    import datetime
    assert app.parse_release_date("AVAILABLE 7/8/2026") == datetime.date(2026, 7, 8)
    assert app.parse_release_date("AVAILABLE 1/8/2025") == datetime.date(2025, 1, 8)


def test_parse_release_date_handles_bare_date_no_prefix():
    """Real value seen on Raptor Reign's page this session (no "AVAILABLE"
    prefix) -- see netsuite scraper's module docstring."""
    import datetime
    assert app.parse_release_date("10/22/2025") == datetime.date(2025, 10, 22)


def test_parse_release_date_returns_none_for_unparseable():
    assert app.parse_release_date("") is None
    assert app.parse_release_date(None) is None
    assert app.parse_release_date("Sometime next year") is None


def test_sigma_release_date_parsed():
    p = _parsed_sigma()
    import datetime
    assert p["release_date"] == datetime.date(2026, 7, 8)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS: {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")
