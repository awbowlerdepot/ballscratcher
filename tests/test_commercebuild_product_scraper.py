"""
Tests for src/commercebuild_product_scraper/app.py.

Fixture HTML below is a reconstruction built from real field
values/structure confirmed via literal `curl` against live
stormbowling.com product pages this session (Storm Alpha Crux, Roto Grip
Gremlin, 900 Global Viking Conquest) -- not a byte-for-byte capture (this
project's established pattern for real-but-large pages, see
test_product_scraper.py's docstring for why: transferring full raw HTML
verbatim has previously triggered this sandbox's anti-exfiltration
safeguard). Every field name, value shape, and structural quirk here
(brand-code prefixes, the single-row/newline-joined PDF table, etc.) is
real and confirmed, not invented -- see
src/commercebuild_product_scraper/app.py's module docstring and
COMMERCEBUILD_SCOPING.md for the full research trail.

Manual-runner pattern, run standalone via
`python3 tests/test_commercebuild_product_scraper.py`.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "commercebuild_product_scraper"))

import app  # noqa: E402

# Real spec-block shape confirmed via curl against
# storm-alpha-crux-bowling-ball this session, reconstructed with the real
# field values/prefixes seen (S_ prefix, MM/DD/YY release date, etc.).
ALPHA_CRUX_HTML = """
<html><head>
<title>Storm Alpha Crux 2026 Bowling Ball | Storm Bowling</title>
<meta property="og:image" content="https://assets.1.commercebuild.com/d186dcd2044cf54d8e48876defef4907/contents/BBMVXA/BBMVXA.png" />
<meta property="product:retailer_item_id" content="BBMVXA" />
</head><body>
<h1>ALPHA CRUX</h1>
<p>SKU: BBMVXA</p>
<div id="div-variant-product"></div>
<script type="module">loadCBCustomisation("div-variant-product", {});</script>
<strong>Brand:</strong> Storm
<strong>Line:</strong> Premier
<strong>Core:</strong> S_AI
<strong>Weight Block:</strong> S_Catalyst_AI
<strong>Finish:</strong> S_2000 Grit
<strong>Durometer:</strong> S_73-75
<strong>Symmetry:</strong> S_Asymmetrical
<strong>Differential:</strong> 0.052
<strong>Flare Potential:</strong> S_High
<strong>Radius of Gyration:</strong> 2.48
<strong>Weight:</strong> 16
<strong>Coverstock:</strong> S_GI26_Solid
<strong>Color:</strong> Black/Turquoise/Violet
<strong>Release Date:</strong> 05/29/26
<strong>Fragrance:</strong> Apple Fritter
<strong>Avail. for Sales Orders:</strong> Yes
<strong>PSA:</strong> 0.017
<h2>DOWNLOADS</h2>
<a href="https://stormproducts.nyc3.cdn.digitaloceanspaces.com/product_pages/Balls/Storm/Alpha_Crux/Storm_adsheet_AlphaCrux-nobleed.pdf">Alpha Crux Ad Sheet</a>
<a href="https://stormproducts.nyc3.cdn.digitaloceanspaces.com/product_pages/Balls/Storm/Alpha_Crux/Alpha%20Crux%20Tech%20Data%20Final.pdf">Alpha Crux Tech Data</a>
</body></html>
"""

# Real shape confirmed via curl against 900-global-viking-conquest-bowling-ball
# this session -- same field set, different brand prefix (G_), confirms
# uniformity across brands.
GLOBAL_900_HTML = """
<html><head>
<meta property="og:image" content="https://assets.1.commercebuild.com/d186dcd2044cf54d8e48876defef4907/contents/BBMGVC/BBMGVC.png" />
<meta property="product:retailer_item_id" content="BBMGVC" />
</head><body>
<h1>VIKING CONQUEST</h1>
<strong>Brand:</strong> 900 Global
<strong>Line:</strong> 900
<strong>Core:</strong> G_AI
<strong>Weight Block:</strong> G_Strobe AI
<strong>Finish:</strong> G_2000 Grit
<strong>Durometer:</strong> G_74-76
<strong>Symmetry:</strong> G_Asymmetrical
<strong>Differential:</strong> 0.05
<strong>Flare Potential:</strong> G_High
<strong>Radius of Gyration:</strong> 2.5
<strong>Weight:</strong> 16
<strong>Coverstock:</strong> G_94_Solid
<strong>Color:</strong> Purple/White/Gray
<strong>Release Date:</strong> 06/26/26
<strong>Avail. for Sales Orders:</strong> Yes
<strong>PSA:</strong> 0.014
</body></html>
"""

URL = "https://www.stormbowling.com/storm-alpha-crux-bowling-ball"


# --- parse_spec_fields / _clean_field_value ---

def test_parse_spec_fields_strips_brand_prefix_and_underscores():
    fields = app.parse_spec_fields(ALPHA_CRUX_HTML)
    assert fields["core"] == "AI"
    assert fields["weight block"] == "Catalyst AI"
    assert fields["symmetry"] == "Asymmetrical"
    assert fields["coverstock"] == "GI26 Solid"


def test_parse_spec_fields_leaves_non_prefixed_values_alone():
    fields = app.parse_spec_fields(ALPHA_CRUX_HTML)
    assert fields["brand"] == "Storm"
    assert fields["release date"] == "05/29/26"
    assert fields["differential"] == "0.052"


def test_parse_spec_fields_global_900_prefix():
    fields = app.parse_spec_fields(GLOBAL_900_HTML)
    assert fields["brand"] == "900 Global"
    assert fields["weight block"] == "Strobe AI"
    assert fields["coverstock"] == "94 Solid"


# --- parse_core_type ---

def test_parse_core_type_asymmetric():
    assert app.parse_core_type("Asymmetrical") == "asymmetric"


def test_parse_core_type_symmetric():
    assert app.parse_core_type("Symmetrical") == "symmetric"


def test_parse_core_type_none_for_missing():
    assert app.parse_core_type(None) is None


# --- parse_coverstock ---

def test_parse_coverstock_solid_reactive():
    assert app.parse_coverstock("GI26 Solid") == {
        "coverstock_material": "reactive_resin",
        "coverstock_type": "solid",
    }


def test_parse_coverstock_pearl():
    assert app.parse_coverstock("V-R1 Pearl") == {
        "coverstock_material": "reactive_resin",
        "coverstock_type": "pearl",
    }


def test_parse_coverstock_returns_none_when_missing():
    assert app.parse_coverstock(None) == {"coverstock_material": None, "coverstock_type": None}


# --- parse_release_date ---

def test_parse_release_date_real_mm_dd_yy_format():
    import datetime
    assert app.parse_release_date("05/29/26") == datetime.date(2026, 5, 29)
    assert app.parse_release_date("07/18/25") == datetime.date(2025, 7, 18)


def test_parse_release_date_returns_none_for_unparseable():
    assert app.parse_release_date("") is None
    assert app.parse_release_date(None) is None
    assert app.parse_release_date("December 2025") is None  # Brunswick's shape, not this platform's


# --- parse_product_name / parse_sku_code / parse_main_image_url / parse_tech_data_pdf_url ---

def test_parse_product_name_from_h1():
    assert app.parse_product_name(ALPHA_CRUX_HTML) == "ALPHA CRUX"


def test_parse_sku_code_from_meta_tag():
    assert app.parse_sku_code(ALPHA_CRUX_HTML) == "BBMVXA"


def test_parse_main_image_url_from_og_image():
    assert app.parse_main_image_url(ALPHA_CRUX_HTML, URL) == (
        "https://assets.1.commercebuild.com/d186dcd2044cf54d8e48876defef4907/contents/BBMVXA/BBMVXA.png"
    )


def test_parse_tech_data_pdf_url_matches_by_link_text_not_filename():
    """Real confirmed shape: Storm's Tech Data PDF is named "Alpha Crux
    Tech Data Final.pdf", Roto Grip's is "Tech Doc_HP3_GREMLIN.pdf" --
    wildly different filenames, but both links' own text contains "Tech
    Data". Also confirms the Ad Sheet link (also a .pdf) is correctly
    NOT matched."""
    url = app.parse_tech_data_pdf_url(ALPHA_CRUX_HTML, URL)
    assert url == (
        "https://stormproducts.nyc3.cdn.digitaloceanspaces.com/product_pages/"
        "Balls/Storm/Alpha_Crux/Alpha%20Crux%20Tech%20Data%20Final.pdf"
    )


def test_parse_tech_data_pdf_url_returns_none_when_absent():
    assert app.parse_tech_data_pdf_url(GLOBAL_900_HTML, URL) is None


# --- parse_product_page (end to end) ---

def test_parse_product_page_alpha_crux_end_to_end():
    parsed = app.parse_product_page(ALPHA_CRUX_HTML, URL)
    assert parsed["name"] == "ALPHA CRUX"
    assert parsed["sku_code"] == "BBMVXA"
    assert parsed["brand_name"] == "Storm"
    assert parsed["core_name"] == "Catalyst AI"
    assert parsed["core_type"] == "asymmetric"
    assert parsed["coverstock_material"] == "reactive_resin"
    assert parsed["coverstock_type"] == "solid"
    assert parsed["color"] == "Black/Turquoise/Violet"
    import datetime
    assert parsed["release_date"] == datetime.date(2026, 5, 29)
    assert parsed["html_weight_lbs"] == 16
    assert parsed["html_rg"] == 2.48
    assert parsed["html_differential"] == 0.052
    assert parsed["tech_data_pdf_url"] is not None


def test_parse_product_page_900_global_brand_field():
    parsed = app.parse_product_page(GLOBAL_900_HTML, URL)
    assert parsed["brand_name"] == "900 Global"
    assert parsed["name"] == "VIKING CONQUEST"


# --- _skus_from_table (real Tech Data PDF table shape) ---

# Exact real shape confirmed via pdfplumber.extract_tables() against the
# real Alpha Crux Tech Data PDF this session: one row, four newline-joined
# cells (weight, RG, Diff, PSA), not one row per weight.
REAL_ALPHA_CRUX_TABLE = [[
    "16 lb\n15 lb\n14 lb\n13 lb\n12 lb",
    "2.48\n2.48\n2.52\n2.56\n2.58",
    "0.052\n0.053\n0.051\n0.034\n0.031",
    "0.017\n0.018\n0.016\n0.011\n0.009",
]]


def test_skus_from_table_real_alpha_crux_shape():
    skus = app._skus_from_table(REAL_ALPHA_CRUX_TABLE)
    assert len(skus) == 5
    by_weight = {s["weight_lbs"]: s for s in skus}
    assert by_weight[16] == {"weight_lbs": 16, "rg": 2.48, "differential": 0.052, "mass_bias": None, "psa": 0.017}
    assert by_weight[12] == {"weight_lbs": 12, "rg": 2.58, "differential": 0.031, "mass_bias": None, "psa": 0.009}
    assert by_weight[14]["rg"] == 2.52


def test_skus_from_table_mismatched_column_lengths_skipped_not_guessed():
    """Real defensive behavior: if a table's columns don't line up
    (e.g. a differently-shaped PDF from a different product template),
    skip it rather than mis-pairing values across weights."""
    bad_table = [["16 lb\n15 lb", "2.48", "0.052\n0.053", "0.017\n0.018"]]
    assert app._skus_from_table(bad_table) == []


def test_skus_from_table_empty_table():
    assert app._skus_from_table([]) == []
    assert app._skus_from_table([[]]) == []


def test_skus_from_table_no_psa_column():
    """Confirms a 3-column table (no PSA) still works -- some PDFs may not
    report PSA at all."""
    table = [["16 lb\n15 lb", "2.48\n2.49", "0.052\n0.053"]]
    skus = app._skus_from_table(table)
    assert len(skus) == 2
    assert skus[0]["psa"] is None


# Exact real shape confirmed via pdfplumber.extract_tables() against the
# real Roto Grip Gremlin Tech Data PDF this deploy's first live smoke
# test -- a real header row followed by one data row per weight, totally
# different from Alpha Crux's single-row/newline-joined shape. This is
# the case that silently produced 0 SKUs (not even a warning) under the
# old row[0]-only, newline-split-only implementation.
REAL_GREMLIN_TABLE = [
    ["WEIGHT", "RG", "DIFF", "PSA"],
    ["16 lbs.", "2.50", ".056", ".011"],
    ["15 lbs.", "2.50", ".058", ".010"],
    ["14 lbs.", "2.54", ".058", ".008"],
    ["13 lbs.", "2.57", ".032", ".010"],
    ["12 lbs.", "2.59", ".029", ".008"],
]


def test_skus_from_table_real_gremlin_long_format_shape():
    skus = app._skus_from_table(REAL_GREMLIN_TABLE)
    assert len(skus) == 5
    by_weight = {s["weight_lbs"]: s for s in skus}
    # No-leading-zero DIFF/PSA values (".056", ".011") must parse as
    # 0.056/0.011, not 56.0/11.0 -- see _to_float's docstring.
    assert by_weight[16] == {"weight_lbs": 16, "rg": 2.50, "differential": 0.056, "mass_bias": None, "psa": 0.011}
    assert by_weight[12] == {"weight_lbs": 12, "rg": 2.59, "differential": 0.029, "mass_bias": None, "psa": 0.008}


def test_skus_from_table_gremlin_header_row_not_mistaken_for_data():
    """The header row itself ("WEIGHT"/"RG"/"DIFF"/"PSA") must never
    produce a spurious SKU -- none of its cells contain a digit, so the
    weight-token match correctly never fires on it."""
    skus = app._skus_from_table(REAL_GREMLIN_TABLE)
    assert all(isinstance(s["weight_lbs"], int) for s in skus)
    assert len(skus) == 5  # not 6 -- header row excluded


# Exact real shape confirmed via pdfplumber.extract_tables() against the
# real Storm Phaze II Tech Data PDF this deploy's first live smoke test --
# same "one row, newline-joined" family as Alpha Crux, but with an extra
# blank leading column (shifting weight/rg/diff over by one index) and no
# PSA column at all. pdfplumber also captured a second, spurious row from
# the page's "DESIGN INTENT:" section heading as part of the same table --
# real, confirmed noise that must be skipped, not parsed as data.
REAL_PHAZE_II_TABLE = [
    [None, "16 lb\n15 lb\n14 lb\n13 lb\n12 lb", "2.48\n2.48\n2.53\n2.59\n2.65", ".051\n.051\n.050\n.045\n.035"],
    ["DESIGN INTENT:", None, None, None],
]


def test_skus_from_table_real_phaze_ii_shifted_columns_no_psa():
    skus = app._skus_from_table(REAL_PHAZE_II_TABLE)
    assert len(skus) == 5
    by_weight = {s["weight_lbs"]: s for s in skus}
    # Old fixed-index code would have read this DIFF value into psa
    # instead -- confirms the real bug is actually fixed, not just that
    # SOME data comes back.
    assert by_weight[16] == {"weight_lbs": 16, "rg": 2.48, "differential": 0.051, "mass_bias": None, "psa": None}
    assert by_weight[12]["differential"] == 0.035
    assert all(s["psa"] is None for s in skus)  # Phaze II genuinely has no PSA column


def test_skus_from_table_phaze_ii_design_intent_row_ignored():
    """The spurious "DESIGN INTENT:" row pdfplumber captured as part of
    the same table must not produce a 6th bogus SKU or raise."""
    skus = app._skus_from_table(REAL_PHAZE_II_TABLE)
    assert len(skus) == 5


# --- _to_float (real bug: no-leading-zero values) ---

def test_to_float_no_leading_zero_real_bug():
    """Real bug found via this deploy's first live smoke test: both
    Gremlin's and Phaze II's actual PDFs use ".051"/".056"-style values
    with no leading zero. The old regex required a digit before the
    decimal point and silently matched only the post-decimal digits,
    turning 0.051 into 51.0 -- a thousand-x corruption that would have
    written obviously-wrong data straight into product_skus."""
    assert app._to_float(".051") == 0.051
    assert app._to_float(".011") == 0.011


def test_to_float_leading_zero_still_works():
    assert app._to_float("0.052") == 0.052
    assert app._to_float("2.48") == 2.48


def test_to_float_negative_no_leading_zero():
    assert app._to_float("-.05") == -0.05


def test_to_float_none_and_empty():
    assert app._to_float(None) is None
    assert app._to_float("") is None
    assert app._to_float("n/a") is None


# --- cross_check_html_vs_pdf ---

def test_cross_check_flags_real_disagreement():
    parsed = {"html_weight_lbs": 16, "html_rg": 2.60, "html_differential": 0.052}  # HTML says 2.60
    pdf_skus = [{"weight_lbs": 16, "rg": 2.48, "differential": 0.052}]  # PDF says 2.48 -- real disagreement
    mismatches = app.cross_check_html_vs_pdf(parsed, pdf_skus)
    rg_mismatch = next(m for m in mismatches if m["field_name"] == "rg_16lb")
    assert rg_mismatch["current_value"] == "2.6"
    assert rg_mismatch["proposed_value"] == "2.48"


def test_cross_check_no_mismatch_when_values_agree():
    parsed = {"html_weight_lbs": 16, "html_rg": 2.48, "html_differential": 0.052}
    pdf_skus = [{"weight_lbs": 16, "rg": 2.48, "differential": 0.052}]
    assert app.cross_check_html_vs_pdf(parsed, pdf_skus) == []


def test_cross_check_handles_decimal_from_postgres():
    """Same real bug this deploy already fixed twice (pdf_parser,
    bowwwl_cross_check): if html_rg/html_differential ever arrive as
    decimal.Decimal (e.g. if this function were ever called with a
    DB-round-tripped value instead of a fresh parse), abs(Decimal - float)
    must not raise TypeError. Both operands are coerced to float inside
    cross_check_html_vs_pdf regardless of what type they arrive as."""
    from decimal import Decimal
    parsed = {"html_weight_lbs": 16, "html_rg": Decimal("2.60"), "html_differential": Decimal("0.052")}
    pdf_skus = [{"weight_lbs": 16, "rg": 2.48, "differential": 0.052}]
    mismatches = app.cross_check_html_vs_pdf(parsed, pdf_skus)
    assert len(mismatches) == 1
    assert mismatches[0]["field_name"] == "rg_16lb"


def test_cross_check_no_matching_weight_returns_empty():
    parsed = {"html_weight_lbs": 16, "html_rg": 2.48, "html_differential": 0.052}
    pdf_skus = [{"weight_lbs": 15, "rg": 2.48, "differential": 0.053}]
    assert app.cross_check_html_vs_pdf(parsed, pdf_skus) == []


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}")
        except Exception as e:
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed")
