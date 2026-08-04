"""
Tests for the HTML product scraper, run against two fixtures built from real
field values captured from brunswickbowling.com. Originally captured during
architecture research via a markdown-converting tool (never raw HTML); this
session re-verified every value and the structural pattern against a literal
raw-HTTP fetch of both real live pages via Claude in Chrome (see the comment
blocks in tests/fixtures/crown_78u.html and defender.html, and
src/product_scraper/app.py's module docstring, for exactly what's now real-
verified vs. still a reconstruction, and the two real bugs that
re-verification found and fixed: day-precision release dates, and PDF
resource links whose own text is a generic "Download" rather than the
resource label).

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

from bs4 import BeautifulSoup  # noqa: E402

from app import (  # noqa: E402
    parse_product_page,
    parse_coverstock,
    parse_weights_available,
    parse_release_date,
    parse_images,
    parse_description,
    _nearby_label_text,
    _resolve_img_src,
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


def test_crown_78u_description_finds_hidden_block(crown_78u):
    """Confirmed live via Claude in Chrome against this same real page --
    see parse_description's docstring and this fixture's own header
    comment for the full trail. The div is visually hidden (class
    "u-hide") but present in the raw server HTML, same as every other
    field this scraper parses."""
    assert crown_78u["description"].startswith(
        "Brunswick introduces the Crown 78U, giving bowlers a new urethane option"
    )
    assert "Tiered Hexagon core shape from the Crown Victory" in crown_78u["description"]


def test_parse_description_returns_none_when_block_absent():
    soup = BeautifulSoup("<html><body><h1>No Description Here</h1></body></html>", "lxml")
    assert parse_description(soup) is None


def test_crown_78u_resources(crown_78u):
    """Real markup puts every PDF link's own text as the generic word
    "Download" -- the actual label ("Crown 78U Info Sheet", etc.) is a
    sibling heading in the link's wrapping div instead, confirmed live
    this session (see app.py's module docstring). This test exercises
    that real shape end to end via the fixture, not just the pure
    _nearby_label_text() helper -- confirms parse_resources() correctly
    resolves info_sheet_url/ball_talker_url/flip_card_url despite the
    link text itself carrying no useful information."""
    assert crown_78u["resources"]["info_sheet_url"].endswith("Crown_78U_Info_Sheet_1025-12.pdf")
    assert crown_78u["resources"]["ball_talker_url"].endswith("Crown_78U_Ball_Talker_1025-11.pdf")
    assert crown_78u["resources"]["flip_card_url"].endswith("Crown_78U_Flip_Card_1025-11.pdf")


def test_crown_78u_unrecognized_pdf_labels_bucket_into_other(crown_78u):
    """Real page also has "Print Ad" and "Ball Motion Comparison Chart
    Poster" PDFs alongside the three this schema tracks by name --
    confirms they're captured (not silently dropped) rather than
    mis-bucketed into one of the three named slots."""
    assert len(crown_78u["resources"]["other"]) == 2


def test_crown_78u_release_date_end_to_end(crown_78u):
    """Real value from Crown 78U's live spec table: day-precision
    "December 11, 2025", not the day-less "Month YYYY" shape
    parse_release_date() originally only handled -- this fixture had the
    real value all along, but nothing checked parse_product_page()'s
    release_date field end-to-end against it until now, which is exactly
    how this bug went undetected. See app.py's module docstring."""
    import datetime
    assert crown_78u["release_date"] == datetime.date(2025, 12, 11)


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


def test_defender_description_none_when_fixture_lacks_hidden_block(defender):
    """defender.html's fixture was never updated with a u-hide description
    block -- unlike crown_78u.html, this isn't claiming the real Defender
    page lacks one, just confirming a missing block doesn't crash parsing
    and leaves the field None, same graceful-miss convention as every
    other optional field this scraper parses."""
    assert defender["description"] is None


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


def test_parse_release_date_day_precision_real_crown_78u_value():
    """The real, live, day-precision shape -- confirmed this session via
    a literal raw-HTTP fetch against Crown 78U's actual page (see app.py's
    module docstring). This is the format that was NOT originally
    supported and silently produced None in production."""
    import datetime
    assert parse_release_date("December 11, 2025") == datetime.date(2025, 12, 11)
    assert parse_release_date("Dec 11, 2025") == datetime.date(2025, 12, 11)


def test_parse_release_date_month_year_only_still_accepted():
    """A day-less "Month YYYY" shape, from an earlier session's
    architecture-doc notes (Crown Victory = April 2025, Crown 78U =
    December 2025) -- possibly just a summarized version of the
    day-precision value rather than a genuinely different page format
    (no live page has actually shown this exact shape), but kept
    supported since it costs nothing and nothing disproves it."""
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


# --- _resolve_img_src: the fix for the real lazy-load-placeholder bug ---
#
# Real bug, found via live production CloudWatch logs (not a hypothetical):
# every ball page has a lazy-loaded "Performance Index" chart image whose
# `src` is an inline transparent SVG placeholder
# (data:image/svg+xml;charset=utf-8,...), with the real image URLs only in
# `srcset`. parse_images() used to read img["src"] directly, so it stored
# the placeholder as source_url -- which then failed downstream in
# image_processor with `InvalidSchema: No connection adapters were found
# for 'data:image/svg+xml...'`. This exact snippet (widths/URLs redacted to
# a shorter but structurally identical example) was captured from
# brunswickbowling.com/products/balls/current/tzone-berry-blast's real raw
# HTML via curl, confirming the shape, not assumed.

_LAZY_PERFORMANCE_INDEX_IMG = """
<img class=""
    loading="lazy"
    src="data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%27680%27%20height%3D%27140%27%20style%3D%27background%3Atransparent%27%2F%3E"
    srcset="https://brunswickbowling.nyc3.cdn.digitaloceanspaces.com/production/transforms/bowlerproducts/Products/Balls/performance-index/3756/ball-pi_680w.png 680w, https://brunswickbowling.nyc3.cdn.digitaloceanspaces.com/production/transforms/bowlerproducts/Products/Balls/performance-index/3756/ball-pi_1360w.png 1360w"
    sizes="100vw"
    alt="Polyester Accurate 99 Performance Index"
    />
"""


def test_resolve_img_src_prefers_highest_res_srcset_over_placeholder():
    soup = BeautifulSoup(_LAZY_PERFORMANCE_INDEX_IMG, "lxml")
    img = soup.find("img")
    src = _resolve_img_src(img, "https://brunswickbowling.com/products/balls/current/tzone-berry-blast")
    assert src == (
        "https://brunswickbowling.nyc3.cdn.digitaloceanspaces.com/production/"
        "transforms/bowlerproducts/Products/Balls/performance-index/3756/ball-pi_1360w.png"
    )
    assert not src.startswith("data:")


def test_resolve_img_src_falls_back_to_src_when_no_srcset():
    soup = BeautifulSoup('<img src="/images/ball.png" alt="Ball">', "lxml")
    img = soup.find("img")
    assert _resolve_img_src(img, "https://brunswickbowling.com/x") == "https://brunswickbowling.com/images/ball.png"


def test_resolve_img_src_returns_none_for_placeholder_with_no_srcset():
    """Belt-and-suspenders case: if a data: placeholder ever appears with no
    srcset at all, there's nothing real to recover -- must return None
    (dropped by parse_images), never the placeholder itself."""
    soup = BeautifulSoup(
        '<img src="data:image/svg+xml;charset=utf-8,%3Csvg%2F%3E" alt="placeholder">',
        "lxml",
    )
    img = soup.find("img")
    assert _resolve_img_src(img, "https://brunswickbowling.com/x") is None


def test_parse_images_skips_placeholder_and_resolves_lazy_image():
    """End-to-end through parse_images(), not just the helper -- confirms
    the lazy performance-index image is captured with its real URL (bucketed
    as "other", since it's neither the first image nor a core-callout) and
    never with the data: placeholder."""
    html = (
        '<img src="/main.png" alt="main product shot">'
        + _LAZY_PERFORMANCE_INDEX_IMG
    )
    soup = BeautifulSoup(html, "lxml")
    images = parse_images(soup, "https://brunswickbowling.com/products/balls/current/tzone-berry-blast")

    assert len(images) == 2
    assert images[0]["image_type"] == "main"
    assert images[0]["source_url"] == "https://brunswickbowling.com/main.png"
    assert images[1]["image_type"] == "other"
    assert images[1]["source_url"].endswith("ball-pi_1360w.png")
    assert all(not img["source_url"].startswith("data:") for img in images)


# --- _nearby_label_text: the fix for the real "Download"-link-text bug ---

def test_nearby_label_text_finds_sibling_heading_in_wrapping_div():
    """Real markup shape, reproduced directly (not through the full
    fixture) -- link's own text is generic, the real label is a sibling
    heading in the same wrapping div."""
    soup = BeautifulSoup(
        '<div><h3>Crown 78U Info Sheet</h3><a href="x.pdf">Download</a></div>',
        "lxml",
    )
    link = soup.find("a")
    assert _nearby_label_text(link) == "Crown 78U Info Sheet Download"


def test_nearby_label_text_two_separate_resources_dont_cross_attribute():
    """Confirms the bounded climb doesn't let one resource's container
    pick up a sibling resource's label -- each real container held
    exactly one PDF link on both pages checked, and this asserts that
    isolation holds even when two such containers sit next to each
    other."""
    soup = BeautifulSoup(
        """
        <div><h3>Info Sheet</h3><a id="a" href="a.pdf">Download</a></div>
        <div><h3>Ball Talker</h3><a id="b" href="b.pdf">Download</a></div>
        """,
        "lxml",
    )
    link_a = soup.find("a", id="a")
    link_b = soup.find("a", id="b")
    assert "info sheet" in _nearby_label_text(link_a).lower()
    assert "ball talker" not in _nearby_label_text(link_a).lower()
    assert "ball talker" in _nearby_label_text(link_b).lower()
    assert "info sheet" not in _nearby_label_text(link_b).lower()


def test_nearby_label_text_falls_back_to_own_text_when_nothing_more_specific():
    """No wrapping element carries extra text within the bound -- falls
    back to the link's own text rather than raising or returning empty."""
    soup = BeautifulSoup('<a href="x.pdf">Download</a>', "lxml")
    link = soup.find("a")
    assert _nearby_label_text(link) == "Download"
