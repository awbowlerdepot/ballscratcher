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
<ul  itemscope itemtype="https://schema.org/BreadcrumbList" id="breadcrumbs">
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem" class="home">
    <a href="https://www.stormbowling.com/" itemprop="item"><span itemprop="name">Home</span></a>
    <meta itemprop="position" content="1" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
    <a href="https://www.stormbowling.com/products/" itemprop="item"><span itemprop="name">Products</span></a>
    <meta itemprop="position" content="2" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
    <a href="https://www.stormbowling.com/products/equipment/" itemprop="item"><span itemprop="name">Equipment</span></a>
    <meta itemprop="position" content="3" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
    <a href="https://www.stormbowling.com/products/equipment/bowling-balls/" itemprop="item"><span itemprop="name">Bowling Balls</span></a>
    <meta itemprop="position" content="4" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem" class="last-child">
    <span itemprop="name">ALPHA CRUX</span>
    <meta itemprop="position" content="5" /></li>
</ul>
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
<div class="std secondary-desc">
<p><strong>AI Core:</strong> The Alpha Intelligence core continues Storm's asymmetric lineage with a stronger shape than the outgoing Crux, giving it more traction in the midlane without giving up backend reaction.</p>
<p><strong>GI26 Solid Coverstock:</strong> The debut of the GI26 line, built for heavy fresh oil with an aggressive, angular finish through the pins.</p>
</div>
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

# REAL INCIDENT fixture: confirmed live via a fetch of
# https://www.stormbowling.com/storm-tropical-surge-bowling-ball-black-cherry
# this session, product 56897c0b-e3ec-4314-a8dc-238e1b8b7a75 -- Al reported
# it had zero product_skus despite the page clearly showing weight/RG/
# differential values. Root cause: its Downloads section links the tech
# data PDF with the text "Tech Sheet: Surge Black/Cherry PDF" -- the ONLY
# "tech data" substring anywhere is inside the PDF's own FILENAME
# (Storm_Tropical_Surge_Black_Cherry_Tech_Data.pdf), never in the link
# text, so the original text-only match missed it entirely. Also
# confirms the other real Downloads-section links (Ad Sheet, ball image,
# 8's/Crazy 8's flyers, resurfacing/drilling guides) are correctly NOT
# matched.
TROPICAL_SURGE_HTML = """
<html><head>
<meta property="og:image" content="https://assets.1.commercebuild.com/d186dcd2044cf54d8e48876defef4907/contents/BT1TQY/BT1TQY.png" />
<meta property="product:retailer_item_id" content="BT1TQY" />
</head><body>
<h1>TROPICAL SURGE BLACK-CHERRY</h1>
<p>SKU: BT1TQY</p>
<strong>Brand:</strong> Storm
<strong>Line:</strong> Tropical
<strong>Core:</strong> S_Light Weight
<strong>Weight Block:</strong> S_Surge Core
<strong>Finish:</strong> S_1500 Grit Polished
<strong>Durometer:</strong> S_73-75
<strong>Symmetry:</strong> S_Symmetrical
<strong>Differential:</strong> 0.024
<strong>Flare Potential:</strong> S_Low
<strong>Radius of Gyration:</strong> 2.58
<strong>Weight:</strong> 15
<strong>Coverstock:</strong> S_Reactor Hybrid
<strong>Color:</strong> Black/Cherry
<strong>Fragrance:</strong> Cherry
<strong>Avail. for Sales Orders:</strong> Yes
<h2>DOWNLOADS</h2>
<a href="https://stormproducts.nyc3.cdn.digitaloceanspaces.com/product_pages/Balls/Storm/Tropical_Surge/Storm_adsheet_TropicalSurge2024_nobleed.pdf">Tropical Surge 2024 Ad Sheet</a>
<a href="https://stormproducts.nyc3.cdn.digitaloceanspaces.com/product_pages/Balls/Storm/Tropical_Surge/storm-tropical-surge-black-cherry-bowling-ball.png">Tropical Surge Black/Cherry Ball Image</a>
<a href="https://stormproducts.nyc3.cdn.digitaloceanspaces.com/product_pages/Balls/Storm/Tropical_Surge/Storm_Tropical_Surge_Black_Cherry_Tech_Data.pdf">Tech Sheet: Surge Black/Cherry PDF</a>
<a href="https://stormproducts.nyc3.cdn.digitaloceanspaces.com/product_pages/Balls/Storm/Tropical_Surge/8s_en_Tropical%20Surge.pdf">Tropical Surge 8's PDF</a>
<a href="https://stormproducts.nyc3.cdn.digitaloceanspaces.com/product_pages/Balls/Storm/Universal_Downloads/Storm_Resurfacing_Guide.pdf">Storm Resurfacing Guide PDF</a>
</body></html>
"""

# Synthetic -- confirms the filename-fallback path (_looks_like_tech_data_
# filename) specifically, for wording this module hasn't seen live yet:
# link text has neither "tech data" nor "tech sheet" in it anywhere, but
# the filename itself contains "Tech_Data".
UNKNOWN_WORDING_TECH_DATA_HTML = """
<html><body>
<h1>MYSTERY BALL</h1>
<h2>DOWNLOADS</h2>
<a href="https://example.com/downloads/Mystery_Ball_Ad_Sheet.pdf">Ad Sheet</a>
<a href="https://example.com/downloads/Mystery_Ball_Tech_Data.pdf">Specifications</a>
</body></html>
"""

# Real <ul id="breadcrumbs"> shape confirmed via curl against
# storm-alpha-crux-bowling-ball (current) this session -- reconstructed
# with the real schema.org markup and item text seen.
CURRENT_BREADCRUMB_HTML = """
<ul  itemscope itemtype="https://schema.org/BreadcrumbList" id="breadcrumbs">
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem" class="home">
    <a href="https://www.stormbowling.com/" itemprop="item"><span itemprop="name">Home</span></a>
    <meta itemprop="position" content="1" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
    <a href="https://www.stormbowling.com/products/" itemprop="item"><span itemprop="name">Products</span></a>
    <meta itemprop="position" content="2" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
    <a href="https://www.stormbowling.com/products/equipment/" itemprop="item"><span itemprop="name">Equipment</span></a>
    <meta itemprop="position" content="3" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
    <a href="https://www.stormbowling.com/products/equipment/bowling-balls/" itemprop="item"><span itemprop="name">Bowling Balls</span></a>
    <meta itemprop="position" content="4" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem" class="last-child">
    <span itemprop="name">ALPHA CRUX</span>
    <meta itemprop="position" content="5" /></li>
</ul>
"""

# Real shape confirmed via curl against storm-absolute-bowling-ball
# (archived) this session -- note "Featured" / "Bowling Balls Archive"
# instead of "Equipment" / "Bowling Balls".
ARCHIVED_BREADCRUMB_HTML = """
<ul  itemscope itemtype="https://schema.org/BreadcrumbList" id="breadcrumbs">
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem" class="home">
    <a href="https://www.stormbowling.com/" itemprop="item"><span itemprop="name">Home</span></a>
    <meta itemprop="position" content="1" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
    <a href="https://www.stormbowling.com/products/" itemprop="item"><span itemprop="name">Products</span></a>
    <meta itemprop="position" content="2" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
    <a href="https://www.stormbowling.com/products/featured/" itemprop="item"><span itemprop="name">Featured</span></a>
    <meta itemprop="position" content="3" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
    <a href="https://www.stormbowling.com/products/featured/bowling-balls-archive/" itemprop="item"><span itemprop="name">Bowling Balls Archive</span></a>
    <meta itemprop="position" content="4" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem" class="last-child">
    <span itemprop="name">ABSOLUTE</span>
    <meta itemprop="position" content="5" /></li>
</ul>
"""

# Real shape confirmed via curl against a Roto Grip bag URL this session --
# a non-ball control, used to confirm classify_product_status() correctly
# returns None (skip) rather than guessing "current".
NON_BALL_BREADCRUMB_HTML = """
<ul  itemscope itemtype="https://schema.org/BreadcrumbList" id="breadcrumbs">
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem" class="home">
    <a href="https://www.stormbowling.com/" itemprop="item"><span itemprop="name">Home</span></a>
    <meta itemprop="position" content="1" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
    <a href="https://www.stormbowling.com/products/" itemprop="item"><span itemprop="name">Products</span></a>
    <meta itemprop="position" content="2" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
    <a href="https://www.stormbowling.com/products/equipment/" itemprop="item"><span itemprop="name">Equipment</span></a>
    <meta itemprop="position" content="3" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
    <a href="https://www.stormbowling.com/products/equipment/bowling-bags/" itemprop="item"><span itemprop="name">Bowling Bags</span></a>
    <meta itemprop="position" content="4" /></li>
  <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem" class="last-child">
    <span itemprop="name">ROTO 3-BALL A-S E ROLLER COMPETITOR</span>
    <meta itemprop="position" content="5" /></li>
</ul>
"""

# Real spec-field shape confirmed via curl against storm-absolute-bowling-ball
# (archived) this session -- only Coverstock/Core/Factory Finish/Color/
# Release Date/Fragrance, no Brand/Weight/RG/Differential/PSA/Symmetry at
# all (confirmed 3-for-3 across all three brands), plus the real
# <!--Tech Data--> HTML comment marker (not a PDF link) and zero .pdf
# hrefs anywhere on the page.
ARCHIVED_ABSOLUTE_HTML = """
<html><head>
<meta property="og:image" content="https://assets.1.commercebuild.com/d186dcd2044cf54d8e48876defef4907/contents/BBMVBS/BBMVBS.png" />
<meta property="product:retailer_item_id" content="BBMVBS" />
</head><body>
""" + ARCHIVED_BREADCRUMB_HTML + """
<h1 itemprop="name">ABSOLUTE</h1>
<div id="div-variant-product"></div>
<script type="module">loadCBCustomisation("div-variant-product", {});</script>
<!--Tech Data-->
<p><strong>Coverstock: </strong>R2S DEEP Hybrid</p>
<p><strong>Core: </strong>Sentinel Asymmetrical Core</p>
<p><strong>Factory Finish: </strong>Reacta Gloss</p>
<p><strong>Color: </strong>Copperhead/Jade/Phantom Black</p>
<p><strong>Release Date: </strong>January 2023</p>
<p><strong>Fragrance: </strong>Orange Cream Soda</p>
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


def test_parse_spec_fields_handles_space_before_closing_tag_real_archived_bug():
    """Real bug found via this session's archived-product research: unlike
    current pages' <strong>Label:</strong> value (space AFTER the closing
    tag), archived pages use <strong>Label: </strong>value (space BEFORE
    it, confirmed real via curl against storm-absolute-bowling-ball). The
    original SPEC_LABEL_RE required the colon immediately before
    </strong> with zero tolerance for that leading space, which would
    have silently returned an empty fields dict for every archived
    product -- no error, just nothing parsed. This is the no-whitespace
    case that must still work after the fix."""
    html = "<strong>Coverstock: </strong>R2S DEEP Hybrid"
    fields = app.parse_spec_fields(html)
    assert fields["coverstock"] == "R2S DEEP Hybrid"


# --- parse_breadcrumb_trail / classify_product_status ---

def test_parse_breadcrumb_trail_current_product():
    trail = app.parse_breadcrumb_trail(CURRENT_BREADCRUMB_HTML)
    assert trail == ["Home", "Products", "Equipment", "Bowling Balls", "ALPHA CRUX"]


def test_parse_breadcrumb_trail_archived_product():
    trail = app.parse_breadcrumb_trail(ARCHIVED_BREADCRUMB_HTML)
    assert trail == ["Home", "Products", "Featured", "Bowling Balls Archive", "ABSOLUTE"]


def test_parse_breadcrumb_trail_not_confused_by_nav_menu():
    """Real gotcha hit this session: an unscoped itemprop="name" search
    over the whole page also matches the site's main nav menu items
    (Company/Products/Events/...), which happen to use the same
    attribute. This must only read the id="breadcrumbs" block."""
    html_with_nav = "<nav><span itemprop='name'>Company</span><span itemprop='name'>Products</span></nav>" + CURRENT_BREADCRUMB_HTML
    trail = app.parse_breadcrumb_trail(html_with_nav)
    assert trail == ["Home", "Products", "Equipment", "Bowling Balls", "ALPHA CRUX"]


def test_parse_breadcrumb_trail_empty_when_missing():
    assert app.parse_breadcrumb_trail("<html><body>no breadcrumbs here</body></html>") == []


def test_classify_product_status_current():
    assert app.classify_product_status(CURRENT_BREADCRUMB_HTML) == "current"


def test_classify_product_status_archived_returns_retired():
    """Real DB enum value is 'retired', not 'archived' -- the site's own
    UI language ("Bowling Balls Archive") differs from this project's
    product_status enum (db/migrations/001_init_schema.sql), same
    'current'/'retired' split Brunswick's Craft-CMS scraper already
    uses."""
    assert app.classify_product_status(ARCHIVED_BREADCRUMB_HTML) == "retired"


def test_classify_product_status_non_ball_returns_none():
    """A bag page must return None (skip), not accidentally classify as
    current/retired just because it has SOME breadcrumb trail."""
    assert app.classify_product_status(NON_BALL_BREADCRUMB_HTML) is None


def test_classify_product_status_no_breadcrumb_returns_none():
    assert app.classify_product_status("<html><body>no breadcrumbs</body></html>") is None


# --- parse_product_page on a real archived-product page (no Brand/Weight/RG/PSA fields, no PDF) ---

def test_parse_product_page_archived_status_is_retired():
    parsed = app.parse_product_page(ARCHIVED_ABSOLUTE_HTML, "https://www.stormbowling.com/storm-absolute-bowling-ball")
    assert parsed["status"] == "retired"


def test_parse_product_page_archived_has_no_brand_field():
    """Confirmed real 3-for-3 across all three brands this session:
    archived pages have no Brand: label at all. Doesn't matter for the DB
    write (brand_id comes from the job, not this field), but
    parse_product_page must not error or fabricate one."""
    parsed = app.parse_product_page(ARCHIVED_ABSOLUTE_HTML, "https://www.stormbowling.com/storm-absolute-bowling-ball")
    assert parsed["brand_name"] is None


def test_parse_product_page_archived_has_no_weight_rg_psa():
    """Confirmed real: archived pages have zero RG:/Diff:/PSA: occurrences
    anywhere and no Weight field -- the JS-locked variant widget is
    present but, unlike current products, there's no Tech Data PDF
    fallback either (see next test), so these stay None rather than being
    guessed at."""
    parsed = app.parse_product_page(ARCHIVED_ABSOLUTE_HTML, "https://www.stormbowling.com/storm-absolute-bowling-ball")
    assert parsed["html_weight_lbs"] is None
    assert parsed["html_rg"] is None
    assert parsed["html_differential"] is None


def test_parse_product_page_archived_has_no_tech_data_pdf():
    """Confirmed real 3-for-3: archived pages have zero .pdf hrefs at
    all -- the "<!--Tech Data-->" marker on these pages is an HTML
    comment introducing an inline spec-field block, not a PDF download
    link, unlike current products."""
    parsed = app.parse_product_page(ARCHIVED_ABSOLUTE_HTML, "https://www.stormbowling.com/storm-absolute-bowling-ball")
    assert parsed["tech_data_pdf_url"] is None


def test_parse_product_page_archived_still_gets_coverstock_and_color():
    """The smaller field set archived pages DO have (Coverstock/Core/
    Factory Finish/Color/Release Date/Fragrance) should still parse
    normally via the same label-driven parse_spec_fields()."""
    parsed = app.parse_product_page(ARCHIVED_ABSOLUTE_HTML, "https://www.stormbowling.com/storm-absolute-bowling-ball")
    assert parsed["coverstock_name"] == "R2S DEEP Hybrid"
    assert parsed["color"] == "Copperhead/Jade/Phantom Black"


def test_parse_product_page_current_status_is_current():
    parsed = app.parse_product_page(ALPHA_CRUX_HTML, URL)
    assert parsed["status"] == "current"


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


# --- _normalize_coverstock_name: real duplicate-data bug Al found in the
# coverstocks table (migration 008/009) -- a manufacturer page adds a TM/
# R/C symbol to a coverstock name sometimes but not always for the exact
# same coverstock, which used to create two coverstocks rows for one real
# coverstock.

def test_normalize_coverstock_name_strips_trademark_symbol():
    assert app._normalize_coverstock_name("Catalyst AI™") == "Catalyst AI"


def test_normalize_coverstock_name_matches_already_clean_text():
    assert app._normalize_coverstock_name("Catalyst AI™") == app._normalize_coverstock_name("Catalyst AI")


def test_normalize_coverstock_name_returns_none_for_empty():
    assert app._normalize_coverstock_name(None) is None
    assert app._normalize_coverstock_name("") is None


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


# --- REAL INCIDENT: "Tech Sheet" wording (Storm Tropical Surge Black/
# Cherry, product 56897c0b-e3ec-4314-a8dc-238e1b8b7a75) was silently
# producing zero product_skus -- see TROPICAL_SURGE_HTML's own comment
# and parse_tech_data_pdf_url's docstring for the full root-cause writeup.

def test_parse_tech_data_pdf_url_matches_tech_sheet_wording():
    url = app.parse_tech_data_pdf_url(TROPICAL_SURGE_HTML, URL)
    assert url == (
        "https://stormproducts.nyc3.cdn.digitaloceanspaces.com/product_pages/"
        "Balls/Storm/Tropical_Surge/Storm_Tropical_Surge_Black_Cherry_Tech_Data.pdf"
    )


def test_parse_tech_data_pdf_url_does_not_match_ad_sheet_or_flyers_for_tech_sheet_fixture():
    # Confirms the other real Downloads-section links (Ad Sheet, ball
    # image, 8's flyer, resurfacing guide) aren't accidentally matched --
    # only the one link whose text actually says "Tech Sheet".
    url = app.parse_tech_data_pdf_url(TROPICAL_SURGE_HTML, URL)
    assert "Tech_Data" in url
    assert "adsheet" not in url.lower()


def test_parse_tech_data_pdf_url_falls_back_to_filename_when_text_matches_no_synonym():
    # Synthetic wording this module has never seen live -- link text is
    # "Specifications", not "Tech Data"/"Tech Sheet" -- but the filename
    # itself contains "Tech_Data", so the fallback still finds it.
    url = app.parse_tech_data_pdf_url(UNKNOWN_WORDING_TECH_DATA_HTML, URL)
    assert url == "https://example.com/downloads/Mystery_Ball_Tech_Data.pdf"


def test_parse_tech_data_pdf_url_still_matches_alpha_crux_tech_data_wording_unchanged():
    # Regression guard: widening the match must not break the original
    # confirmed-real "Tech Data" wording.
    url = app.parse_tech_data_pdf_url(ALPHA_CRUX_HTML, URL)
    assert url == (
        "https://stormproducts.nyc3.cdn.digitaloceanspaces.com/product_pages/"
        "Balls/Storm/Alpha_Crux/Alpha%20Crux%20Tech%20Data%20Final.pdf"
    )


# --- parse_description: confirmed live via Claude in Chrome against the
# real Storm Absolute page this session -- see this module's docstring
# note on parse_description for the full verification trail. Uses the
# ALPHA_CRUX_HTML/GLOBAL_900_HTML fixtures above (secondary-desc div added
# to Alpha Crux's fixture, deliberately absent from 900 Global's -- same
# "confirm both the match and the graceful miss" pattern as
# test_parse_tech_data_pdf_url_returns_none_when_absent).

def test_parse_description_finds_secondary_desc_block():
    description = app.parse_description(ALPHA_CRUX_HTML)
    assert description is not None
    assert "Alpha Intelligence core" in description
    assert "GI26 Solid Coverstock" in description
    assert "Storm's asymmetric lineage" in description


def test_parse_description_returns_none_when_absent():
    assert app.parse_description(GLOBAL_900_HTML) is None


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
    assert "Alpha Intelligence core" in parsed["description"]


def test_parse_product_page_900_global_brand_field():
    parsed = app.parse_product_page(GLOBAL_900_HTML, URL)
    assert parsed["brand_name"] == "900 Global"
    assert parsed["name"] == "VIKING CONQUEST"
    assert parsed["description"] is None


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


# --- _skus_from_text (real Tech Data PDF that isn't table-shaped at all) ---

# Real extract_text() output confirmed via pdfplumber against Storm
# Lightning Storm Clear's actual Tech Data PDF -- pdfplumber's table
# detection couldn't cleanly split this into columns at all (product
# details AND the weight table both landed in one blob per its own
# extract_tables() call), so this is the raw-text fallback's real input.
# Note the interleaved contact-info junk lines (phone/email/website) at
# the same vertical position as the real data rows -- confirmed real,
# not constructed for the test.
REAL_LIGHTNING_TEXT = """LIGHTNING STORM
CLEAR
COVERSTOCK: Clear Polyester
WEIGHT BLOCK: Traditional 3-piece Core
FACTORY FINISH: 3500-grit Polished
BALL COLOR: Multi
FLAREPOTENTIAL: Low
WEIGHTS: 12-16 lbs.
SKU: VCL
Digital Imaging Core Technology
LBS RG DIFF
16 2.68 0.006
800-369-4402
15 2.69 0.006
tech@stormbowling.com
14 2.69 0.006
13 2.71 0.005
www.stormbowling.com
12 2.72 0.005"""


def test_skus_from_text_real_lightning_storm_clear():
    skus = app._skus_from_text(REAL_LIGHTNING_TEXT)
    assert len(skus) == 5
    by_weight = {s["weight_lbs"]: s for s in skus}
    assert by_weight[16] == {"weight_lbs": 16, "rg": 2.68, "differential": 0.006, "mass_bias": None, "psa": None}
    assert by_weight[12] == {"weight_lbs": 12, "rg": 2.72, "differential": 0.005, "mass_bias": None, "psa": None}


def test_skus_from_text_ignores_phone_email_website_lines():
    """The real interleaved junk lines must never produce spurious SKUs
    or crash the parser -- "800-369-4402" in particular starts with
    digits, which is exactly the case this needs to get right."""
    skus = app._skus_from_text(REAL_LIGHTNING_TEXT)
    weights = [s["weight_lbs"] for s in skus]
    assert weights == sorted(weights, reverse=True)  # 16,15,14,13,12 in order, nothing extra spliced in
    assert 800 not in weights  # "800-369-4402" must never be read as a weight


def test_skus_from_text_handles_optional_psa_column():
    text = "WEIGHT RG DIFF PSA\n16 2.50 0.056 0.011\n15 2.50 0.058 0.010"
    skus = app._skus_from_text(text)
    assert len(skus) == 2
    assert skus[0] == {"weight_lbs": 16, "rg": 2.50, "differential": 0.056, "mass_bias": None, "psa": 0.011}


def test_skus_from_text_empty_string():
    assert app._skus_from_text("") == []
    assert app._skus_from_text("no numbers here at all") == []


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
