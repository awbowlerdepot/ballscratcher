"""
Tests for src/woocommerce_product_scraper/app.py, run against
tests/fixtures/swag_fusion.html -- a reconstruction using real field
values captured from swagbowling.com/product/swag-fusion-bowling-ball/
this session (see that fixture's header comment for exactly what's real).
Manual-runner pattern, run standalone via
`python3 tests/test_woocommerce_product_scraper.py`.
"""
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "woocommerce_product_scraper"))

import app  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
FUSION_URL = "https://www.swagbowling.com/product/swag-fusion-bowling-ball/"


def _parsed_fusion():
    html = (FIXTURES / "swag_fusion.html").read_text()
    return app.parse_product_page(html, FUSION_URL)


def test_basic_fields():
    p = _parsed_fusion()
    assert p["name"] == "SWAG Fusion Bowling Ball"
    assert p["status"] == "current"
    assert p["color"] == "Black, Pink, Purple"
    assert p["core_name"] == "SWAG Fusion Core"
    assert p["coverstock_name"] == "SWAG Xplode Solid Reactive"
    assert p["factory_finish"] == "3000 Grit"
    assert p["performance_level_raw"] == "Modern Performance Line"
    assert p["release_date_raw"] == "January 2025"


# --- parse_description: confirmed live via Claude in Chrome against the
# real Executioner Solid product page this session -- see parse_
# description's docstring and swag_fusion.html's fixture comment for the
# full verification trail.

def test_parse_description_finds_short_description_block():
    p = _parsed_fusion()
    assert p["description"] == (
        "SWAG proudly presents the Modern Performance Line, a revolutionary "
        "series that seamlessly combines cutting-edge performance with "
        "innovative symmetrical core technology."
    )


def test_parse_description_returns_none_when_block_absent():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup("<html><body><h1>No Description Here</h1></body></html>", "lxml")
    assert app.parse_description(soup) is None


def test_parse_description_returns_none_for_external_product():
    """External/affiliate listings never reach parse_description at all --
    parse_product_page returns the {"external_product": True} sentinel
    before parsing attributes (see that function's docstring). Confirms
    the sentinel path doesn't include a "description" key that could be
    mistaken for a real (but empty) one."""
    from bs4 import BeautifulSoup
    external_html = "<html><body><h1>SWAG Apex Pearl Bowling Ball</h1></body></html>"
    parsed = app.parse_product_page(external_html, "https://www.swagbowling.com/product/swag-apex-pearl-bowling-ball/")
    assert parsed == {
        "url": "https://www.swagbowling.com/product/swag-apex-pearl-bowling-ball/",
        "name": "SWAG Apex Pearl Bowling Ball",
        "external_product": True,
    }


def test_coverstock_split_across_two_fields():
    """Material comes from "Bowling Ball Coverstock Type" (Reactive),
    type comes from keyword-matching "Bowling Ball Cover Name" (contains
    "Solid") -- the real, confirmed structural difference from Brunswick
    documented in the module docstring."""
    p = _parsed_fusion()
    assert p["coverstock_material"] == "reactive_resin"
    assert p["coverstock_type"] == "solid"


# --- _normalize_coverstock_name: real duplicate-data bug Al found in the
# coverstocks table (migration 008/009) -- a manufacturer page adds a TM/
# R/C symbol to a coverstock name sometimes but not always for the exact
# same coverstock, which used to create two coverstocks rows for one real
# coverstock.

def test_normalize_coverstock_name_strips_trademark_symbol():
    assert app._normalize_coverstock_name("Reactor Solid™") == "Reactor Solid"


def test_normalize_coverstock_name_matches_already_clean_text():
    assert app._normalize_coverstock_name("Reactor Solid™") == app._normalize_coverstock_name("Reactor Solid")


def test_normalize_coverstock_name_returns_none_for_empty():
    assert app._normalize_coverstock_name(None) is None
    assert app._normalize_coverstock_name("") is None


def test_weights_available_from_multivalue_attribute():
    p = _parsed_fusion()
    assert p["weights_available"] == (13, 16)


def test_sku_defaults_to_15lb_with_real_values():
    p = _parsed_fusion()
    assert len(p["skus"]) == 1
    sku = p["skus"][0]
    assert sku["weight_lbs"] == 15
    assert sku["rg"] == 2.54
    assert sku["differential"] == 0.036
    assert sku["mass_bias"] is None  # real value is "N/A" -- symmetric core


def test_resources_extracts_real_dropbox_links():
    p = _parsed_fusion()
    assert p["resources"]["info_sheet_url"].endswith("swag-fusion-flyer-dec2024.pdf?rlkey=k31nzd00h61gu32xg7j02q27x&e=1&st=jk4q0xig&dl=0")
    assert p["resources"]["shelf_talker_url"].endswith("swag-fusion-solid-shelf-dec2024.pdf?rlkey=gqkawb3uelurx6rob18lbhyjh&e=1&st=vxs4bppg&dl=0")


def test_images_main_and_core():
    p = _parsed_fusion()
    assert len(p["images"]) == 2
    assert p["images"][0]["image_type"] == "main"
    assert p["images"][0]["source_url"].endswith("FUSION-600x600.png")
    assert p["images"][1]["image_type"] == "core_callout"
    assert p["images"][1]["source_url"].endswith("FUSION-CORE-600x571.png")


# Real, confirmed structure of a WooCommerce "External/Affiliate Product"
# listing -- checked via curl against swagbowling.com/product/swag-apex-
# pearl-bowling-ball/ this deploy's first live smoke test. Has a real
# <h1> title, but no "Additional information" attributes table and no
# woocommerce-tabs section at all -- confirmed via the product-type-
# external CSS class and grep coming back empty for "production" and
# "Additional information" against the actual page. Roughly a third of
# SWAG's real catalog hit this on the first live run.
EXTERNAL_PRODUCT_HTML = """
<html><body>
<div id="product-16226" class="product type-product post-16226 status-publish instock product_cat-bowling-balls product-type-external">
<div class="product-title-container is-large"><h1 class="product-title product_title entry-title">
SWAG APEX Pearl Bowling Ball</h1></div>
</div>
</body></html>
"""


def test_external_product_returns_sentinel_not_none_status():
    """Real bug found via this deploy's first live smoke test: about a
    third of SWAG's real catalog are WooCommerce external/affiliate
    listings with no attribute table at all -- these used to silently
    produce status=None, which crashed the DB write on the products.status
    NOT NULL constraint. parse_product_page() must return the
    external_product sentinel instead of a dict with status=None."""
    parsed = app.parse_product_page(EXTERNAL_PRODUCT_HTML, "https://www.swagbowling.com/product/swag-apex-pearl-bowling-ball/")
    assert parsed["external_product"] is True
    assert parsed["name"] == "SWAG APEX Pearl Bowling Ball"
    assert "status" not in parsed  # must not carry a None status through


def test_external_product_sentinel_does_not_apply_to_real_fusion_page():
    """Confirms the real Fusion fixture (which DOES have a full attributes
    table) is never mistakenly treated as an external-product listing."""
    p = _parsed_fusion()
    assert "external_product" not in p
    assert p["status"] == "current"


# --- Helper functions tested directly, beyond what the fixture exercises ---

def test_parse_mass_bias_rejects_na():
    assert app.parse_mass_bias("N/A") is None


def test_parse_mass_bias_accepts_real_number():
    """Unconfirmed against a real asymmetric SWAG page this session (see
    module docstring's disclosed gap) -- this just confirms the function
    itself handles a numeric value correctly if/when one is encountered."""
    assert app.parse_mass_bias("0.028") == 0.028


def test_parse_mass_bias_rejects_qualitative_value():
    """If SWAG expresses mass bias qualitatively for asymmetric balls
    (e.g. "Strong") rather than numerically, this must return None rather
    than fabricate a number -- see module docstring."""
    assert app.parse_mass_bias("Strong") is None


def test_parse_status_current():
    assert app.parse_status("In Production") == "current"


def test_parse_status_retired():
    assert app.parse_status("Discontinued") == "retired"


def test_parse_status_unknown_value_returns_none():
    assert app.parse_status("Something Else") is None


def test_parse_weights_available_handles_single_weight():
    assert app.parse_weights_available("15LB") == (15, 15)


def test_parse_weights_available_returns_none_for_empty():
    assert app.parse_weights_available("") is None
    assert app.parse_weights_available(None) is None


def test_parse_release_date_real_swag_fusion_value():
    import datetime
    assert app.parse_release_date("January 2025") == datetime.date(2025, 1, 1)


def test_parse_release_date_returns_none_for_unparseable():
    assert app.parse_release_date("") is None
    assert app.parse_release_date(None) is None
    assert app.parse_release_date("Q1 2025") is None


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS: {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")
