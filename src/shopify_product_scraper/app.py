"""
Product scraper for the Shopify brand family (Hammer Bowling to start,
joined by Track and Ebonite -- see shopify_url_discovery/app.py's module
docstring for the platform confirmation).

TWO MARKUP FAMILIES, not one -- confirmed real this session via live
fetches against trackbowling.com and ebonite.com: Hammer's BALL SPECS/
RG-DIFF sections are <ul><li><strong> lists (see parse_ball_specs/
parse_rg_diff_list below), but Track and Ebonite both use plain HTML
<table>s instead, under different heading text ("Specifications"/"Core
Numbers" instead of "BALL SPECS"/"RG / DIFF") and even a different heading
tag (Track: <h3>, Ebonite: <h2>). Reusing the <ul>-based parsers unchanged
against these would not error -- _find_section's old single-prefix,
<ul>/<p>-only signature would just silently return None for every Track/
Ebonite product (heading text never matches "BALL SPECS", and even a
matching heading's next sibling is a <table> it wasn't looking for),
producing a fully "successful" scrape with completely empty specs/core/
coverstock/skus for every product -- a worse failure mode than the
"E "-prefix bug (see parse_ball_specs's docstring) because nothing about
it would look wrong until someone noticed the data gap. _find_section now
checks both <h2>/<h3> and accepts multiple heading-text prefixes, and
parse_product_page dispatches to parse_ball_specs_table/
parse_rg_diff_table instead of the <ul>-based pair whenever the located
section's tag is a <table> -- see those two functions' docstrings for the
real cross-brand formatting differences they tolerate (label cells
plain text on Track, <strong>-wrapped on Ebonite; ASY column present or
absent depending on whether that ball's core is asymmetric).

Fetches each product via Shopify's own {product_url}.json convention
(confirmed real this session against
https://hammerbowling.com/products/black-widow-3-0-dynasty.json -- same
data as the HTML page, just structured, no HTML parsing needed to locate
it) rather than fetching and parsing the rendered HTML page the other
three families' scrapers all do. The spec data itself, though, still lives
inside one field of that JSON -- `body_html`, a themed HTML fragment
Hammer's product-page template renders directly into the page -- so this
module still does BeautifulSoup parsing, just against that fragment
instead of a full fetched page.

Two real, confirmed structural differences from every other family in this
codebase that shaped this module's design:

1. RG/DIFF/ASY (when the core is asymmetric) are already given per-weight
   directly in body_html's "RG / DIFF" (or "RG / DIFF / ASY") list --
   confirmed real across every product inspected this session, old and
   new. No PDF step needed at all for this platform's core numbers, unlike
   Brunswick (mass bias only in the PDF) -- see parse_core_type() for how
   asymmetric vs. symmetric is inferred when there's no explicit "CORE
   TYPE" field (only seen on some older retired listings, e.g. the real
   3-D Offset fixture).
2. Hammer's product page has NO current/retired signal of its own --
   unlike Brunswick's URL path or SWAG's "Production-status" attribute,
   nothing in body_html says whether a ball is still for sale. Status is
   determined once, at discovery time, by which Shopify collection the
   product was found under (see shopify_url_discovery/app.py) and stored
   on discovered_urls.status_path. This module reads that back via
   get_status_for_url() rather than re-deriving it from the page, and
   defaults to 'current' if the URL isn't in discovered_urls yet at all
   (e.g. a manual/direct scrape of a URL nobody's ever discovered through
   the normal collection-crawl path -- treated as "presumably still live"
   rather than failing the NOT NULL products.status constraint).

Real, confirmed formatting inconsistencies across eras that parse_ball_specs/
parse_rg_diff_list are built to tolerate (all three shapes seen live this
session, real fixtures for each in tests/fixtures/):
- Modern (Black Widow 3.0 Dynasty, Spawn, Fallout): "16 lb - RG (2.510)
  DIFF (0.048) ASY (0.015)", BALL SPECS labels like COLOR/CORE/COVERSTOCK/
  COVER TYPE all present as <strong>LABEL</strong><span>value</span> --
  though the value sometimes spills outside the <span> as plain trailing
  text (Fallout's "COVER TYPE" is <span>Solid</span> Reactive, not fully
  inside the span) -- parse_ball_specs reads everything that comes after
  the label's own <strong> tag (siblings, not a text-offset slice) to
  survive this. See parse_ball_specs's own docstring for a real bug this
  module shipped to production with: an earlier version sliced by
  character count instead, which broke on real markup's whitespace
  formatting and corrupted every spec field for every Hammer product.
- Older retired (3-D Offset): adds an explicit "CORE TYPE" field
  ("Asymmetric") BALL SPECS doesn't always carry on newer balls, and heading
  text "RG / DIFF / ASY" instead of "RG / DIFF" -- find_section() matches
  on "RG" as a prefix rather than either exact heading, so both match.
- Very old retired (Absolut Curve, 2018): completely different RG/DIFF
  list format -- "#10 RG (2.72) Diff (.031)" (a bare "#<weight>" instead of
  "<weight> lb -", lowercase "Diff", no ASY at all since this is a
  symmetric-core ball) and a "FACTORY FINISH"/"BEST LANE CONDITION"/
  "AVAILABLE WEIGHTS" label set instead of "FINISH"/"LANE CONDITION"/
  "WEIGHTS" -- parse_ball_specs's label lookup and
  WEIGHT_RE/RG_RE/DIFF_RE/ASY_RE regexes are all written to match either
  variant.

No resources/PDF-download parsing here at all (unlike every other
family's scraper) -- there's nothing downstream that needs those PDF URLs
for this platform (see point 1 above), so capturing them would just be
dead data with no consumer, not a real gap.
"""
import logging
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Canonical BALL SPECS label -> parsed dict key. Both the modern and the
# very-old (Absolut Curve era) label spellings map to the same key, since
# they mean the same thing -- see module docstring.
BALL_SPEC_LABEL_MAP = {
    "PERFORMANCE": "performance_level_raw",
    "PART NUMBER": "part_number",
    "COLOR": "color",
    "CORE": "core_name",
    "CORE TYPE": "core_type_raw",
    "COVERSTOCK": "coverstock_name",
    "COVER TYPE": "coverstock_type_raw",
    "FINISH": "factory_finish",
    "FACTORY FINISH": "factory_finish",
    "WEIGHTS": "weights_raw",
    "AVAILABLE WEIGHTS": "weights_raw",
    "LANE CONDITION": "lane_condition",
    "BEST LANE CONDITION": "lane_condition",
    "REACTION": "reaction",
    "WARRANTY": "warranty",
    "RELEASE DATE": "release_date_raw",
}

COVERSTOCK_MATERIAL_KEYWORDS = [
    ("urethane", "urethane"),
    ("polyester", "polyester_plastic"),
    ("plastic", "polyester_plastic"),
    ("reactive", "reactive_resin"),
]
COVERSTOCK_TYPE_KEYWORDS = [
    ("solid", "solid"),
    ("pearl", "pearl"),
    ("hybrid", "hybrid"),
]

WEIGHT_RE = re.compile(r"#?(\d{1,2})\s*(?:lb)?", re.IGNORECASE)
RG_RE = re.compile(r"RG\D*?([\d.]+)", re.IGNORECASE)
DIFF_RE = re.compile(r"DIFF\D*?([\d.]+)", re.IGNORECASE)
ASY_RE = re.compile(r"ASY\D*?([\d.]+)", re.IGNORECASE)
WEIGHT_TOKEN_RE = re.compile(r"(\d{1,2})")


def fetch_product_json(url: str, timeout: int = 30) -> dict:
    """Fetches {url}.json -- Shopify's built-in convention for getting a
    product's structured data without parsing the rendered page. Kept
    separate from parsing so tests can feed real fixture JSON without a
    network call."""
    import requests

    resp = requests.get(f"{url}.json", headers={"User-Agent": "bowling-scraper/1.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["product"]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _find_section(soup: BeautifulSoup, heading_prefixes):
    """Finds the <h2>/<h3> whose cleaned, upper-cased text starts with any
    of heading_prefixes, and returns its next sibling tag (the <ul>, <p>,
    or <table> that actually holds the section's content). Matching by
    heading text content rather than any CSS class/position -- same "match
    by content, not markup" approach every other scraper in this codebase
    already uses, needed here too since "RG / DIFF" vs. "RG / DIFF / ASY"
    vs. "CORE NUMBERS" varies by product/brand (see module docstring).

    Both <h2> and <h3> are checked, and heading_prefixes accepts multiple
    strings, because Track and Ebonite (confirmed live, real fetches
    against trackbowling.com and ebonite.com) use different heading tags
    and text than Hammer for the same two sections: Hammer is
    <h3>BALL SPECS</h3>, Track is <h3>Specifications</h3>, Ebonite is
    <h2>Specifications</h2> -- and Hammer's core-numbers heading is
    "RG / DIFF" (or "RG / DIFF / ASY") while Track/Ebonite's is
    <h2 or h3>Core Numbers</h2>. A single string parameter couldn't match
    both "BALL SPECS" and "SPECIFICATIONS" (different first word), or both
    "RG" and "CORE NUMBERS" (no shared prefix), hence the sequence."""
    if isinstance(heading_prefixes, str):
        heading_prefixes = (heading_prefixes,)
    for heading in soup.find_all(["h2", "h3"]):
        text = _clean(heading.get_text()).upper()
        if any(text.startswith(prefix) for prefix in heading_prefixes):
            return heading.find_next_sibling(["ul", "p", "table"])
    return None


def parse_ball_specs(specs_ul) -> dict:
    """Returns {canonical_key: raw_value_text}, normalized through
    BALL_SPEC_LABEL_MAP so callers never need to know which era's label
    spelling a given product used.

    Reads the value by walking strong.next_siblings (everything in the LI
    that comes *after* the label's own <strong> tag) rather than slicing
    li.get_text() by len(label_text) -- that slicing approach was the
    original implementation and shipped to production with a real,
    confirmed off-by-one bug: real Hammer markup is pretty-printed with a
    newline between <li> and <strong> (e.g.
    "<li>\\n<strong>CORE</strong><span> </span>Scandal</li>", confirmed
    live against https://hammerbowling.com/products/scandal.json), so
    li.get_text() includes that leading "\\n" while strong.get_text()
    (used as the slice length) does not. Slicing by len(label_text) then
    starts one character short of where the label actually ends, landing
    on the label's own last character -- e.g. "CORE" produced a leading
    "E ", "COLOR" would have produced a leading "R ". This shipped and
    corrupted every BALL_SPEC_LABEL_MAP field for every Hammer product in
    production (all 219) before being caught, via cores.name -- see
    tests/fixtures/hammer_scandal.json for the real captured body_html and
    tests/test_shopify_product_scraper.py's regression tests for the
    before/after values.

    Walking siblings instead of slicing by character count sidesteps the
    whole class of bug (immune to any whitespace before/inside <strong>)
    while still handling the two real layout quirks the old approach was
    built for: no whitespace between <strong> and <span> at all (Black
    Widow 3.0 Dynasty's "CORE" li is
    <strong>CORE</strong><span>Gas Mask</span>), and a value that spills
    outside the <span> as trailing plain text (Fallout's "COVER TYPE" li
    is <strong>COVER TYPE</strong><span>Solid</span> Reactive) -- both are
    just siblings of <strong>, span or bare NavigableString alike, and get
    concatenated in document order either way."""
    if specs_ul is None:
        return {}
    raw = {}
    for li in specs_ul.find_all("li", recursive=False):
        strong = li.find("strong")
        if strong is None:
            continue
        label = _clean(strong.get_text()).upper()
        canonical = BALL_SPEC_LABEL_MAP.get(label)
        if canonical is None:
            continue
        value = _clean("".join(
            sib.get_text() if hasattr(sib, "get_text") else str(sib)
            for sib in strong.next_siblings
        ))
        if value:
            raw[canonical] = value
    return raw


def parse_rg_diff_list(rg_ul) -> list:
    """Parses the per-weight RG/DIFF/ASY list. WEIGHT_RE/RG_RE/DIFF_RE/
    ASY_RE are all written to tolerate both the modern "16 lb - RG (2.510)
    DIFF (0.048) ASY (0.015)" shape and the very old "#10 RG (2.72) Diff
    (.031)" shape (see module docstring) -- weight is always the first
    number token in the LI's text either way, so a single leftmost
    WEIGHT_RE.search() finds it correctly without needing to know which
    era's format this particular LI is in."""
    if rg_ul is None:
        return []
    skus = []
    for li in rg_ul.find_all("li", recursive=False):
        text = _clean(li.get_text())
        if not text:
            continue
        weight_match = WEIGHT_RE.search(text)
        if not weight_match:
            continue
        rg_match = RG_RE.search(text)
        diff_match = DIFF_RE.search(text)
        asy_match = ASY_RE.search(text)
        skus.append({
            "weight_lbs": int(weight_match.group(1)),
            "rg": float(rg_match.group(1)) if rg_match else None,
            "differential": float(diff_match.group(1)) if diff_match else None,
            "mass_bias": float(asy_match.group(1)) if asy_match else None,
        })
    return skus


def parse_ball_specs_table(specs_table) -> dict:
    """Table-shaped counterpart to parse_ball_specs, for Track/Ebonite
    (confirmed live against trackbowling.com and ebonite.com): both use a
    plain <table><tr><td>label</td><td>value</td></tr>...</table> for the
    "Specifications" section instead of Hammer's <ul><li><strong>. Track's
    label cells are bare text ("<td>Performance</td>"), Ebonite's wrap the
    label in <strong> ("<td><strong>Performance</strong></td>") -- both
    read identically via cell.get_text(), so no brand-specific branching
    needed here. Returns {} the same way parse_ball_specs does for a
    missing section -- real for Ebonite's oldest novelty listings (e.g.
    "Angry Birds"), which have no Specifications table at all, just plain
    marketing paragraphs with at most one inline spec mentioned in
    passing; nothing to structure-parse there, same as any other
    data-sparse retired product."""
    if specs_table is None:
        return {}
    raw = {}
    for tr in specs_table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        label = _clean(cells[0].get_text()).upper()
        canonical = BALL_SPEC_LABEL_MAP.get(label)
        if canonical is None:
            continue
        value = _clean(cells[1].get_text())
        if value:
            raw[canonical] = value
    return raw


def parse_rg_diff_table(rg_table) -> list:
    """Table-shaped counterpart to parse_rg_diff_list, for Track/Ebonite's
    "Core Numbers" table (confirmed live): first row is a header
    (Weight/RG/DIFF, with ASY as a fourth column only for asymmetric
    cores -- same "ASY present" signal parse_core_type already relies on,
    just sourced from a header row's column count instead of a per-LI
    "ASY (...)" substring). Column position is read from the header text
    rather than assumed fixed, since Ebonite's Turbo X NU/Game Breaker 5
    Hybrid tables omit the ASY column entirely (symmetric cores) while
    Track's three modern fixtures and Ebonite's Spartan Pearl all include
    it (asymmetric) -- get_text() on each header cell strips whatever
    <p>/<strong> wrapping that particular brand/era uses, same as the data
    cells below it."""
    if rg_table is None:
        return []
    rows = rg_table.find_all("tr")
    if not rows:
        return []

    header_cells = [_clean(td.get_text()).upper() for td in rows[0].find_all("td")]
    column_keys = []
    for header in header_cells:
        if header.startswith("WEIGHT"):
            column_keys.append("weight_lbs")
        elif header.startswith("RG"):
            column_keys.append("rg")
        elif header.startswith("DIFF"):
            column_keys.append("differential")
        elif header.startswith("ASY"):
            column_keys.append("mass_bias")
        else:
            column_keys.append(None)

    skus = []
    for tr in rows[1:]:
        cells = [_clean(td.get_text()) for td in tr.find_all("td")]
        if not cells:
            continue
        sku = {"weight_lbs": None, "rg": None, "differential": None, "mass_bias": None}
        for key, text in zip(column_keys, cells):
            if key is None or not text:
                continue
            if key == "weight_lbs":
                match = WEIGHT_RE.search(text)
                sku["weight_lbs"] = int(match.group(1)) if match else None
            else:
                try:
                    sku[key] = float(text)
                except ValueError:
                    sku[key] = None
        if sku["weight_lbs"] is not None:
            skus.append(sku)
    return skus


def parse_coverstock(coverstock_name: str, coverstock_type_raw: str) -> dict:
    """Both material (reactive/urethane/polyester) and type (solid/pearl/
    hybrid) are inferred by keyword search across the combined COVERSTOCK
    + COVER TYPE text, e.g. Black Widow 3.0 Dynasty's "HK22 - Cohesion
    Solid" + "Solid Reactive" -> material=reactive_resin, type=solid.
    Unlike SWAG (see woocommerce_product_scraper.parse_coverstock), Hammer
    doesn't cleanly split material into its own dedicated field -- both
    keyword sets are searched across the same combined text here."""
    combined = f"{coverstock_name or ''} {coverstock_type_raw or ''}".lower()

    material = None
    for keyword, value in COVERSTOCK_MATERIAL_KEYWORDS:
        if keyword in combined:
            material = value
            break

    cs_type = None
    for keyword, value in COVERSTOCK_TYPE_KEYWORDS:
        if keyword in combined:
            cs_type = value
            break

    return {"coverstock_material": material, "coverstock_type": cs_type}


def parse_core_type(core_type_raw: str, skus: list):
    """Prefers the explicit "CORE TYPE" field when a product has one (only
    seen on some older retired listings this session, e.g. the real 3-D
    Offset fixture). Otherwise infers from whether any parsed SKU carries
    an ASY (mass bias) value -- present only for asymmetric cores, per
    every real product inspected this session -- rather than leaving
    core_type unset just because the page doesn't spell it out directly."""
    if core_type_raw:
        lowered = core_type_raw.lower()
        if "asymmetric" in lowered:
            return "asymmetric"
        if "symmetric" in lowered:
            return "symmetric"
    if skus:
        return "asymmetric" if any(sku["mass_bias"] is not None for sku in skus) else "symmetric"
    return None


def parse_weights_available(weights_raw: str):
    """Parses "16-12 Pounds" / "16-10 Pounds" / "10-16 LBS" into
    (low, high) -- same (low, high) output shape as
    product_scraper.parse_weights_available /
    woocommerce_product_scraper.parse_weights_available."""
    if not weights_raw:
        return None
    weights = [int(w) for w in WEIGHT_TOKEN_RE.findall(weights_raw)]
    if not weights:
        return None
    return (min(weights), max(weights))


def parse_release_date(release_date_raw: str):
    """"January 15, 2026" / "July 16, 2026" style -- confirmed real across
    every current-ball product inspected this session. Older retired
    listings don't always have a RELEASE DATE field at all (e.g. Absolut
    Curve/Absolut Flip), which is fine -- this just returns None."""
    if not release_date_raw:
        return None
    try:
        return datetime.strptime(release_date_raw.strip(), "%B %d, %Y").date()
    except ValueError:
        return None


def classify_image(alt: str, position: int) -> str:
    """Shopify gives real position + descriptive alt text directly on
    every image, unlike the Craft-CMS/WooCommerce families where image
    role has to be guessed from filename patterns or raw tag order. Real,
    confirmed alt-text convention across every product inspected this
    session: the main product photo's alt describes the ball's colors,
    core-callout images' alt text literally contains the word "core"
    (e.g. "The Black Widow 3.0 Dynasty core for sixteen to fourteen pound
    bowling balls."), and the performance-graphic image's alt contains
    "performance" -- matched by keyword rather than position, so this
    survives a product having more or fewer than the usual 4 images.
    "performance" is checked before "core" on purpose: the real badge
    image's own alt text also happens to mention "Core" as one of its
    labeled stats (e.g. "...with an 8.75 Finish, an 8.75 Core, an 11
    Cover..."), which would otherwise misclassify it as a core_callout --
    confirmed by a real test failure against this exact fixture."""
    lowered = (alt or "").lower()
    if "performance" in lowered:
        return "performance_badge"
    if "core" in lowered:
        return "core_callout"
    if position == 1:
        return "main"
    return "other"


def parse_images(product: dict) -> list:
    images = []
    for img in product.get("images", []):
        src = img.get("src")
        if not src:
            continue
        alt = _clean(img.get("alt") or "")
        images.append({
            "image_type": classify_image(alt, img.get("position", 0)),
            "source_url": src,
            "alt": alt,
        })
    return images


def parse_description(soup: BeautifulSoup) -> str:
    """Takes the first substantial (>=60 chars of text) <p> paragraph
    found in body_html. Real Hammer product pages open with a YouTube-
    embed <h3>, sometimes a marketing-tagline <h3>, then one or more
    descriptive paragraphs -- confirmed inconsistent across real fetches
    this session whether those paragraphs carry a "p1" CSS class (Black
    Widow 3.0 Dynasty, Fallout, Deep Ocean Vibe all do; the 2018-era
    Absolut Curve/Absolut Flip listings use plain untitled <p> tags) -- so
    this matches on paragraph substance, not a class name, before the
    structured BALL SPECS section kicks off. The 60-char threshold is
    deliberately higher than it might need to be: Absolut Curve's real
    fixture opens with three short marketing-teaser <p> tags ("Tough
    look? Check.", "Aggressive Roll? Check.", "Devastating backend
    reaction? You bet." -- the longest of the three is 39 chars) before
    its real description paragraph, and a lower threshold picks one of
    those teasers instead (confirmed by a real test failure against this
    exact fixture). Safe against accidentally grabbing the DOWNLOADS
    section's link-only paragraph instead: that <p> only ever appears
    later in document order (after BALL SPECS/RG-DIFF), and
    find_all("p") returns document order, so the real description is
    always reached first."""
    for p in soup.find_all("p"):
        text = _clean(p.get_text())
        if len(text) >= 60:
            return text
    return None


def parse_product_page(product: dict, url: str) -> dict:
    """Dispatches to the <ul>-based or <table>-based spec/core-numbers
    parser depending on which one _find_section actually located --
    Hammer uses <ul><li>, Track/Ebonite use <table> (see
    parse_ball_specs_table/parse_rg_diff_table's docstrings). Checking
    section.name rather than hardcoding by brand keeps this brand-agnostic
    the same way the rest of this module already is; a future Shopify
    brand landing on either shape needs no changes here."""
    soup = BeautifulSoup(product.get("body_html") or "", "lxml")

    specs_section = _find_section(soup, ("BALL SPECS", "SPECIFICATIONS"))
    if specs_section is not None and specs_section.name == "table":
        raw = parse_ball_specs_table(specs_section)
    else:
        raw = parse_ball_specs(specs_section)

    rg_section = _find_section(soup, ("RG", "CORE NUMBERS"))
    if rg_section is not None and rg_section.name == "table":
        skus = parse_rg_diff_table(rg_section)
    else:
        skus = parse_rg_diff_list(rg_section)

    coverstock = parse_coverstock(raw.get("coverstock_name"), raw.get("coverstock_type_raw"))

    return {
        "url": url,
        "name": product.get("title"),
        "color": raw.get("color"),
        "part_number": raw.get("part_number"),
        "core_name": raw.get("core_name"),
        "core_type": parse_core_type(raw.get("core_type_raw"), skus),
        "coverstock_name": raw.get("coverstock_name"),
        "coverstock_material": coverstock["coverstock_material"],
        "coverstock_type": coverstock["coverstock_type"],
        "factory_finish": raw.get("factory_finish"),
        "weights_available": parse_weights_available(raw.get("weights_raw")),
        "release_date_raw": raw.get("release_date_raw"),
        "release_date": parse_release_date(raw.get("release_date_raw")),
        "performance_level_raw": raw.get("performance_level_raw"),
        "skus": skus,
        "images": parse_images(product),
        "description": parse_description(soup),
    }


# ---------------------------------------------------------------------
# Lambda handler + DB write. Same split/orchestration pattern as
# product_scraper/app.py -- see that module for the reasoning (pure
# parsing above, tested; mechanical DB/SQS below, deferred-imported).
# ---------------------------------------------------------------------

import json
import os


def get_db_connection():
    import boto3
    import psycopg2

    secret_arn = os.environ["DB_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    secret = json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])

    return psycopg2.connect(
        host=secret["host"],
        port=secret.get("port", 5432),
        dbname=secret["dbname"],
        user=secret["username"],
        password=secret["password"],
    )


def get_status_for_url(conn, url: str) -> str:
    """Reads back the current/retired classification
    shopify_url_discovery/app.py already resolved at discovery time (via
    Shopify collection membership) and stored on
    discovered_urls.status_path -- see this module's docstring for why
    that's necessary on this platform. Defaults to 'current' when the URL
    isn't in discovered_urls at all (e.g. a manual/direct scrape of a URL
    the normal collection-crawl hasn't run across yet) rather than leaving
    status null, since products.status is NOT NULL."""
    with conn.cursor() as cur:
        cur.execute("select status_path from discovered_urls where url = %s", (url,))
        row = cur.fetchone()
    if row is None or row[0] is None:
        return "current"
    return row[0]


def get_or_create_core_id(conn, brand_id: str, core_name, core_type=None):
    """Same helper as product_scraper.get_or_create_core_id -- duplicated
    rather than shared, same reasoning as publish_messages elsewhere in
    this project (each Lambda here is its own independent CodeUri
    package). Unlike woocommerce_product_scraper, this one DOES have a
    real core_type most of the time (see parse_core_type)."""
    if not core_name:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into cores (brand_id, name, core_type)
            values (%s, %s, %s)
            on conflict (brand_id, name) do update set
                core_type = coalesce(cores.core_type, excluded.core_type)
            returning id
            """,
            (brand_id, core_name, core_type),
        )
        return cur.fetchone()[0]


def get_or_create_coverstock_id(conn, brand_id: str, coverstock_name, material=None, cs_type=None):
    """Same helper as product_scraper.get_or_create_coverstock_id --
    duplicated rather than shared, same reasoning as
    get_or_create_core_id above. See migration 008 for why this table
    exists."""
    if not coverstock_name:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into coverstocks (brand_id, name, material, type)
            values (%s, %s, %s, %s)
            on conflict (brand_id, name) do update set
                material = coalesce(coverstocks.material, excluded.material),
                type = coalesce(coverstocks.type, excluded.type)
            returning id
            """,
            (brand_id, coverstock_name, material, cs_type),
        )
        return cur.fetchone()[0]


def upsert_product(conn, brand_id: str, status: str, parsed: dict) -> dict:
    """Same shape/return value as product_scraper.upsert_product /
    woocommerce_product_scraper.upsert_product -- status is passed in
    separately (from get_status_for_url()) rather than being part of
    parsed{}, since it never comes from the page itself on this platform
    (see module docstring)."""
    weights_range = None
    if parsed["weights_available"]:
        low, high = parsed["weights_available"]
        weights_range = f"[{low},{high}]"

    core_id = get_or_create_core_id(conn, brand_id, parsed.get("core_name"), parsed.get("core_type"))
    coverstock_id = get_or_create_coverstock_id(
        conn, brand_id, parsed.get("coverstock_name"),
        parsed.get("coverstock_material"), parsed.get("coverstock_type"),
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into products (
                brand_id, name, url, color, coverstock_material, coverstock_type,
                coverstock_name, factory_finish, part_number, weights_available,
                status, source_platform, release_date, description, discontinued_detected_at,
                core_id, coverstock_id
            )
            values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::int4range, %s, 'shopify', %s, %s,
                case when %s = 'retired' then now() else null end,
                %s, %s
            )
            on conflict (url) do update set
                name = excluded.name,
                color = excluded.color,
                coverstock_material = excluded.coverstock_material,
                coverstock_type = excluded.coverstock_type,
                coverstock_name = excluded.coverstock_name,
                factory_finish = excluded.factory_finish,
                part_number = excluded.part_number,
                weights_available = excluded.weights_available,
                status = excluded.status,
                release_date = coalesce(excluded.release_date, products.release_date),
                description = coalesce(excluded.description, products.description),
                core_id = coalesce(excluded.core_id, products.core_id),
                coverstock_id = coalesce(excluded.coverstock_id, products.coverstock_id),
                discontinued_detected_at = case
                    when excluded.status = 'retired' and products.status <> 'retired' then now()
                    when excluded.status = 'current' then null
                    else products.discontinued_detected_at
                end,
                updated_at = now()
            returning id
            """,
            (
                brand_id, parsed["name"], parsed["url"], parsed["color"],
                parsed["coverstock_material"], parsed["coverstock_type"],
                parsed["coverstock_name"], parsed["factory_finish"], parsed["part_number"],
                weights_range, status, parsed["release_date"], parsed["description"],
                status, core_id, coverstock_id,
            ),
        )
        product_id = cur.fetchone()[0]

        for sku in parsed["skus"]:
            cur.execute(
                """
                insert into product_skus (product_id, weight_lbs, rg, differential, mass_bias, source)
                values (%s, %s, %s, %s, %s, 'html')
                on conflict (product_id, weight_lbs) do update set
                    rg = excluded.rg,
                    differential = excluded.differential,
                    mass_bias = coalesce(excluded.mass_bias, product_skus.mass_bias),
                    updated_at = now()
                """,
                (product_id, sku["weight_lbs"], sku["rg"], sku["differential"], sku["mass_bias"]),
            )

        pending_image_jobs = []
        for image in parsed["images"]:
            cur.execute(
                """
                insert into product_images (product_id, image_type, source_url)
                values (%s, %s, %s)
                on conflict (product_id, source_url) do update set image_type = excluded.image_type
                returning id, stored_url
                """,
                (product_id, image["image_type"], image["source_url"]),
            )
            image_id, stored_url = cur.fetchone()
            if stored_url is None:
                pending_image_jobs.append({"product_image_id": str(image_id), "source_url": image["source_url"]})

    conn.commit()
    return {"product_id": product_id, "pending_image_jobs": pending_image_jobs}


def build_image_process_messages(pending_image_jobs: list) -> list:
    return [json.dumps(job) for job in pending_image_jobs]


def publish_messages(sqs_client, queue_url: str, message_bodies: list) -> int:
    """Duplicated from product_scraper/app.py rather than shared -- see
    that module's docstring for why."""
    sent = 0
    for i in range(0, len(message_bodies), 10):
        chunk = message_bodies[i:i + 10]
        entries = [{"Id": str(idx), "MessageBody": body} for idx, body in enumerate(chunk)]
        sqs_client.send_message_batch(QueueUrl=queue_url, Entries=entries)
        sent += len(chunk)
    return sent


def _extract_jobs(event: dict) -> list:
    """Same two-shape support (SQS batch or direct invoke) as
    product_scraper's _extract_jobs, and the same {"url", "brand_id"} job
    shape every other family's scraper accepts -- including from
    admin_api.queue_rescrape's manual/backfill trigger path."""
    if "Records" in event:
        return [(json.loads(r["body"]), r["messageId"]) for r in event["Records"]]
    return [(event, None)]


def _process_one(job: dict, sqs_client) -> dict:
    url = job["url"]
    brand_id = job["brand_id"]

    logger.info("Scraping %s", url)
    product = fetch_product_json(url)
    parsed = parse_product_page(product, url)

    conn = get_db_connection()
    try:
        status = get_status_for_url(conn, url)
        result = upsert_product(conn, brand_id, status, parsed)
    finally:
        conn.close()

    product_id = result["product_id"]
    logger.info("Upserted product %s (%d SKUs, status=%s)", product_id, len(parsed["skus"]), status)

    image_queue_url = os.environ.get("IMAGE_PROCESS_QUEUE_URL")
    image_jobs_published = 0
    if result["pending_image_jobs"] and image_queue_url:
        messages = build_image_process_messages(result["pending_image_jobs"])
        image_jobs_published = publish_messages(sqs_client, image_queue_url, messages)

    return {
        "product_id": str(product_id),
        "sku_count": len(parsed["skus"]),
        "image_jobs_published": image_jobs_published,
    }


def handler(event, context):
    """Handles both an SQS-triggered batch (ShopifyProductScrapeQueue,
    populated by ShopifyUrlDiscoveryFunction) and a direct/manual
    invocation with {"url": "...", "brand_id": "..."}, same
    batchItemFailures pattern as product_scraper's handler."""
    jobs = _extract_jobs(event)

    sqs_client = None
    if os.environ.get("IMAGE_PROCESS_QUEUE_URL"):
        import boto3

        sqs_client = boto3.client("sqs")

    results = []
    batch_item_failures = []
    for job, message_id in jobs:
        try:
            results.append(_process_one(job, sqs_client))
        except Exception:
            logger.exception("Failed to scrape/upsert job: %r", job)
            if message_id is not None:
                batch_item_failures.append({"itemIdentifier": message_id})
            else:
                raise

    response = {"statusCode": 200, "body": json.dumps({"results": results})}
    if "Records" in event:
        response["batchItemFailures"] = batch_item_failures
    return response
