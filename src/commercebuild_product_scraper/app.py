"""
Product scraper for the commercebuild brand family: Storm, Roto Grip, and
900 Global, all served from one site (stormbowling.com) on the
commercebuild/XM Symphony platform. See COMMERCEBUILD_SCOPING.md at the
repo root for the full research trail this module is built from -- real
`curl` output against live product pages, not just a summary.

**Now handles BOTH current and archived (DB status='retired') products**,
via classify_product_status() below, which reads each page's own
breadcrumb trail. This is a real correction to COMMERCEBUILD_SCOPING.md's
original archived-template finding, not an extension of it -- that
research (done in an earlier session) concluded archived pages used a
structurally different template: no Brand field, no JS variant shell, and
the full per-weight RG/Diff/PSA table sitting directly in raw HTML. A
later session's real curl against three archived products (one per
brand: Storm Absolute, Roto Grip TNT, 900 Global Altered Reality)
confirmed that finding is now STALE -- the live site has apparently been
redesigned since. Today, archived pages use the IDENTICAL
`<strong>Label:</strong>` spec-field template and the same JS-locked
`div-variant-product`/loadCBCustomisation weight-variant widget as
current pages. No `RG:`/`Diff:`/`PSA:` label pattern exists anywhere in
real archived-page HTML (grepped for directly, zero matches, all three
brands). The real, confirmed differences for archived pages are just:
- No `Brand:` field (confirmed 3-for-3) -- doesn't matter for this
  scraper's DB writes, since brand_id already comes from which per-brand
  queue discovered the URL (see commercebuild_url_discovery.py), not from
  a parsed field.
- A smaller spec-field set: only Coverstock/Core/Factory
  Finish/Color/Release Date(/Fragrance sometimes) -- no
  Weight/Differential/Radius of Gyration/PSA/Symmetry/Line/Avail. for
  Sales Orders fields at all.
- Zero Tech Data PDF links (confirmed 3-for-3, `.pdf` href count == 0 on
  every archived page checked). Combined with the JS-locked variant
  widget and the total absence of RG/Diff/PSA anywhere in raw HTML, this
  means **archived products have NO per-weight SKU data obtainable by
  this scraper via any method** -- not a bug, a real disclosed platform
  limitation, same "log and skip gracefully, don't guess" spirit as 900
  Global's image-based current-product PDFs (see parse_tech_data_pdf's
  docstring). product_skus ends up empty for every archived product;
  everything else (name, SKU code, coverstock, core, color, release date,
  main image) still gets scraped normally.

**Also resolves COMMERCEBUILD_SCOPING.md's other open item.** The
"Bowling Balls Archive" collection listing's own product links 404ing on
a bare request turned out to have a simple root cause: that collection
URL 302-redirects to `/user/login` (confirmed via curl -sD -- it's gated
behind auth now). URL discovery for archived products doesn't use that
collection listing at all anymore -- see
commercebuild_url_discovery.py's module docstring for the real
replacement (sitemap_products.xml, public, confirmed to list archived
products directly in flat canonical form).

**Non-ball products (bags, apparel, accessories) share the same
brand-prefixed flat URL shape as balls** (confirmed real, e.g.
roto-grip-classic-hoodie, roto-grip-3-ball-roller-bag-competitor), so
commercebuild_url_discovery.py's sitemap-based discovery can't reliably
filter them out by URL alone. classify_product_status() below is the
real filter: it reads each page's own breadcrumb trail (confirmed real
shape, e.g. "Home / Products / Equipment / Bowling Bags / <NAME>" for a
bag vs. "Home / Products / Equipment / Bowling Balls / <NAME>" for a
ball) and returns None for anything that isn't a ball page.
_process_one() skips those with a sentinel result, same pattern as
woocommerce_product_scraper.py's external_product skip -- no DB write,
no error, no DLQ retry.

**Confirmed real via curl this session** (not the readability-tool view,
which would have missed the JS-locked variant data the same way it missed
Brunswick's lazy-load placeholder bug):

1. Every current product page (Storm Alpha Crux, Roto Grip Gremlin, 900
   Global Viking Conquest -- one per brand) shares the identical spec
   block shape: `<strong>Label:</strong> value` pairs for Brand, Line,
   Core, Weight Block, Finish, Durometer, Symmetry, Differential, Flare
   Potential, Radius of Gyration, Weight, Coverstock, Color, Release Date,
   Fragrance, Avail. for Sales Orders, PSA, and sometimes MatchMaker
   App/MatchMaker. Fields aren't 100% uniform (MatchMaker only appeared on
   Roto Grip's page) -- parsing is label-driven and tolerant of
   missing/extra fields, same philosophy as the Craft-CMS scraper.
2. Field VALUES carry a single-letter brand-code prefix baked in by
   commercebuild's custom-field system -- "S_" (Storm), "R_" (Roto Grip),
   "G_" (900 Global) -- e.g. `Symmetry: S_Asymmetrical`,
   `Weight Block: S_Catalyst_AI`. Stripped by _clean_field_value() below.
3. The weight shown in that spec block (`Weight: 16` on every product
   checked) is the ONLY weight a plain HTTP GET ever sees.
   `<div id="div-variant-product"></div>` -- the element that would carry
   other weights' data -- is a genuinely empty shell in raw HTML,
   populated only by a client-side JS module
   (`loadCBCustomisation`/`storage.googleapis.com/cb-customisations-dev`).
   No `<select>` tag exists anywhere in any checked page's raw HTML
   either. This is why the "Tech Data" PDF (see parse_tech_data_pdf below)
   is required reading, not a nice-to-have cross-check like it is for
   Brunswick.
4. Zero `data:` placeholder images found (`grep -c "data:image"` = 0 on
   every current-product page curled this session) -- the Brunswick
   lazy-load bug doesn't reproduce here, at least not on current-product
   pages. Main image comes from the `og:image` meta tag, which was
   present and correct on every page checked.
5. The "Tech Data" PDF's real table structure (confirmed by opening one
   by hand, then by running pdfplumber against it) is NOT one row per
   weight -- it's a single table row whose four cells each contain
   newline-joined values in matching order, e.g.:
     weight cell: "16 lb\\n15 lb\\n14 lb\\n13 lb\\n12 lb"
     RG cell:     "2.48\\n2.48\\n2.52\\n2.56\\n2.58"
   See parse_tech_data_pdf() for the real column layout (weight, RG,
   Diff, PSA -- no mass_bias column observed on the one PDF checked).

**Confirmed via this deploy's first live smoke test (real CloudWatch logs
+ real pdfplumber checks against the actual failing PDFs, not guessed):**
- Tech Data PDF table shape is genuinely NOT uniform across the catalog.
  Three real, different shapes confirmed (Alpha Crux/Ion Pro Solid one
  way, Roto Grip Gremlin a completely different header+rows way, Storm
  Phaze II a column-shifted variant of the first with no PSA column at
  all) -- see _skus_from_table()'s docstring for the full detail and the
  general column-detection approach that now handles all three instead
  of assuming one.
- 900 Global's Viking Conquest Tech Data PDF is genuinely image-based (0
  extractable chars, confirmed via pdfplumber) -- not a shape bug, a real
  platform limitation this scraper can't recover from without OCR (not
  implemented). See parse_tech_data_pdf()'s docstring.
- _to_float() had a real, separate, more serious bug: its regex required
  a digit before the decimal point, so real values with no leading zero
  (".051", confirmed on both Gremlin's and Phaze II's actual DIFF/PSA
  columns) silently became 51.0 instead of 0.051 -- a thousand-x
  corruption, not caught by any test since every fixture this session
  happened to use leading-zero values. Fixed.

**Not yet confirmed, flagged rather than assumed:**
- The exact raw tag wrapping the product name/H1 and the Downloads
  section's link markup were only ever seen through the readability-tool
  view, not raw curl -- parse_product_name() is built against reasonable,
  standard-HTML assumptions (a real `<h1>`) but hasn't been independently
  smoke-tested the way parse_tech_data_pdf_url() now has (confirmed
  working correctly against real Gremlin/Viking Conquest/Phaze II link
  text this deploy).
- coverstock/core-type parsing below is a reasonable-effort mapping from
  the real field values seen this session, not exhaustively checked
  against every product in the catalog.
- Whether a fourth, still-undiscovered Tech Data PDF table shape exists
  beyond the three found so far -- _skus_from_table()'s content-based
  column detection is general rather than hardcoded to these three, but
  that's a design choice to be more robust against one, not a guarantee.
"""
import io
import logging
import re
from datetime import datetime
from urllib.parse import urljoin

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_TOLERANCE = 0.01  # PSA/RG typically reported to 2-3 decimals; a little looser than Brunswick's 0.001 since these are cross-source (HTML's rounded display value vs the PDF's own rounded value), not two reads of the same source.

# <strong>Label:</strong> value  /  <b>Label:</b> value -- confirmed real
# against curled Storm/Roto Grip/900 Global current-product pages this
# session. Real bug found via this session's archived-product research:
# archived pages format the SAME label:value pattern with the whitespace
# on the OTHER side of the colon -- <strong>Coverstock: </strong>value
# (space before the closing tag) instead of current pages'
# <strong>Coverstock:</strong> value (space after) -- confirmed via curl
# against storm-absolute-bowling-ball. The original regex required the
# colon immediately before </strong> with zero tolerance for that leading
# space, which would have silently returned an empty fields dict for
# every archived product (no error, no warning -- parse_spec_fields()
# would have just found nothing, exactly the kind of silent failure this
# project's real bugs have repeatedly turned out to be). The `\s*` added
# before the closing tag below covers both real formats.
SPEC_LABEL_RE = re.compile(r"<(?:strong|b)>([A-Za-z. ]+):\s*</(?:strong|b)>\s*([^<]*)")

# Brand-code prefixes confirmed real on every field value checked this
# session: "S_" (Storm), "R_" (Roto Grip), "G_" (900 Global).
_BRAND_PREFIX_RE = re.compile(r"^[SRG]_")


def _clean_field_value(value: str) -> str:
    """Strips the commercebuild brand-code prefix (S_/R_/G_) and replaces
    underscores with spaces, e.g. "S_Catalyst_AI" -> "Catalyst AI"."""
    value = _BRAND_PREFIX_RE.sub("", value.strip())
    return value.replace("_", " ").strip()


def parse_spec_fields(html: str) -> dict:
    """Returns {lowercased label: cleaned value} for every
    <strong>Label:</strong> value pair found. Label-driven, not
    position/CSS-driven -- tolerant of a product missing or adding fields
    (confirmed real variation: only Roto Grip's checked product had
    MatchMaker fields)."""
    fields = {}
    for label, value in SPEC_LABEL_RE.findall(html):
        fields[label.strip().lower()] = _clean_field_value(value)
    return fields


def _to_float(value):
    """Real bug found via this deploy's first live smoke test: the old
    pattern (`-?\\d+\\.?\\d*`) requires a digit before the decimal point,
    so real Tech Data PDF values with no leading zero -- confirmed real on
    both Roto Grip Gremlin's and Storm Phaze II's actual DIFF/PSA columns,
    e.g. ".051", ".056" -- matched only the digits *after* the dot
    ("051") and silently returned 51.0 instead of 0.051. A thousand-x
    corruption that would have written obviously-wrong-but-plausible-
    looking numbers straight into product_skus. The `\\.\\d+` alternative
    below covers the no-leading-zero case explicitly."""
    if value is None:
        return None
    match = re.search(r"-?(?:\d+\.?\d*|\.\d+)", str(value))
    return float(match.group()) if match else None


def parse_core_type(symmetry_value):
    """Symmetry field values seen: "Asymmetrical", "Symmetrical" (after
    _clean_field_value strips the brand prefix)."""
    if not symmetry_value:
        return None
    v = symmetry_value.lower()
    if "asymmetric" in v:
        return "asymmetric"
    if "symmetric" in v:
        return "symmetric"
    return None


def parse_coverstock(coverstock_value):
    """Real values seen this session (post _clean_field_value):
    "GI26 Solid" (Storm Alpha Crux), "V-R1 Pearl" (Roto Grip Gremlin),
    "94 Solid" (900 Global Viking Conquest). Same
    material-defaults-to-reactive_resin-unless-urethane-mentioned
    reasoning as the Craft-CMS scraper's parse_coverstock, since every
    coverstock value seen so far is a reactive-resin family cover; revisit
    if a real urethane product is checked."""
    if not coverstock_value:
        return {"coverstock_material": None, "coverstock_type": None}

    v = coverstock_value.lower()
    if "urethane" in v:
        material = "urethane"
    elif "polyester" in v or "plastic" in v:
        material = "polyester_plastic"
    else:
        material = "reactive_resin"

    if "hybrid" in v:
        cov_type = "hybrid"
    elif "pearl" in v:
        cov_type = "pearl"
    elif "solid" in v:
        cov_type = "solid"
    else:
        cov_type = None

    return {"coverstock_material": material, "coverstock_type": cov_type}


def parse_release_date(raw):
    """Real format seen this session: "MM/DD/YY", e.g. "05/29/26",
    "07/18/25", "06/26/26" -- notably different from Brunswick's
    "Month D, YYYY" shape, confirmed on all three brands' products
    checked."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return datetime.strptime(raw, "%m/%d/%y").date()
    except ValueError:
        return None


def parse_product_name(html: str) -> str:
    """First real <h1> on the page. Seen through the readability tool as
    a clean "# ALPHA CRUX" / "# GREMLIN" heading; NOT yet confirmed via
    raw curl that it's literally an <h1> tag -- flagged in the module
    docstring as a first-deploy smoke-test target."""
    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.I)
    return m.group(1).strip() if m else None


def parse_sku_code(html: str) -> str:
    """From the product:retailer_item_id meta tag -- confirmed present
    and correct (matched the visible "SKU: BBMVXA" text) on every page
    checked this session, via the readability tool's meta-tag summary.
    Matches either attribute order/quoting commonly used for this
    OpenGraph-style tag."""
    m = re.search(
        r'<meta[^>]+(?:property|name)=["\']product:retailer_item_id["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.I,
    )
    if m:
        return m.group(1).strip()
    # attribute order can be reversed
    m = re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']product:retailer_item_id["\']',
        html, re.I,
    )
    return m.group(1).strip() if m else None


def parse_main_image_url(html: str, base_url: str) -> str:
    """From the og:image meta tag -- confirmed present and correct on
    every product page checked this session (readability tool's
    meta-og:image summary matched the real hero image)."""
    m = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.I,
    )
    if not m:
        m = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            html, re.I,
        )
    return urljoin(base_url, m.group(1)) if m else None


# class="std secondary-desc" -- confirmed live via Claude in Chrome against
# the real Storm Absolute page: the marketing description (e.g. "Sentinel(tm)
# Core: In most cases, an extra slug..." / "R2S (tm) DEEP Hybrid Coverstock:
# R2S Deep is cleaner through the front...") sits in a div with this class,
# already present in the raw server HTML (checked via a literal fetch() of
# the page's own response body from inside a live tab, not the JS-rendered
# DOM -- same verification method used for Brunswick's hidden description,
# see product_scraper/app.py's parse_description). Non-greedy match up to
# the first </div> after the opening tag, same assumption this file's
# breadcrumb regex above already makes: no nested <div> between open and
# close (only <p>/<strong> were present on the one real page checked). A
# false negative here just leaves description null, not a crash.
_SECONDARY_DESC_RE = re.compile(r'class="[^"]*\bsecondary-desc\b[^"]*"[^>]*>(.*?)</div>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_description(html: str) -> str:
    """See _SECONDARY_DESC_RE's comment above. Strips inline tags
    (<p>/<strong>/...) and collapses whitespace, e.g. multiple paragraphs
    covering the core and coverstock separately become one flowing block
    of prose -- fine for feeding to Bedrock as grounding context later,
    which is the only planned consumer."""
    m = _SECONDARY_DESC_RE.search(html)
    if not m:
        return None
    text = _TAG_RE.sub(" ", m.group(1))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


# Real Downloads-section link-text wording confirmed to vary by product:
# "Tech Data" (Alpha Crux) and, per a REAL INCIDENT below, "Tech Sheet"
# (Storm Tropical Surge Black/Cherry). Kept as a small, evidence-based
# synonym list rather than guessing at other plausible wordings this
# module hasn't actually seen -- see _looks_like_tech_data_filename below
# for the fallback that covers wording not yet confirmed real.
TECH_DATA_TEXT_SYNONYMS = ("tech data", "tech sheet")


def _looks_like_tech_data_filename(href: str) -> bool:
    """Last-resort fallback for parse_tech_data_pdf_url when no link's
    text matches TECH_DATA_TEXT_SYNONYMS -- strips spaces/%20/hyphens/
    underscores and checks for "techdata" in the filename itself. This
    module's own original docstring already found filenames vary more
    wildly than link text ("Alpha Crux Tech Data Final.pdf" vs "Tech
    Doc_HP3_GREMLIN.pdf"), so this is deliberately a fallback of last
    resort, not the primary signal -- only reached when the text-based
    pass finds nothing at all."""
    name = href.rsplit("/", 1)[-1].lower()
    normalized = re.sub(r"[\s_\-]|%20", "", name)
    return "techdata" in normalized


def parse_tech_data_pdf_url(html: str, base_url: str):
    """Finds the "Tech Data" PDF link in the Downloads section, primarily
    by LINK TEXT content (TECH_DATA_TEXT_SYNONYMS, case-insensitive),
    falling back to filename pattern matching
    (_looks_like_tech_data_filename) only if no link's text matches any
    known synonym.

    REAL INCIDENT: Al reported product 56897c0b-e3ec-4314-a8dc-238e1b8b7a75
    (Storm Tropical Surge Black/Cherry) had zero product_skus despite its
    real page (stormbowling.com) clearly showing weight/RG/differential
    values. Root cause, confirmed via a live fetch of that exact page:
    its Downloads section links the correct PDF with the text "Tech
    Sheet: Surge Black/Cherry PDF" -- this function's original text match
    only recognized the literal substring "tech data", so it never
    matched here, tech_data_pdf_url came back None, parse_tech_data_pdf
    was never called, and pdf_skus stayed empty -- upsert_product's `for
    sku in pdf_skus:` loop then simply inserts zero product_skus rows,
    with no exception anywhere in the pipeline (the products row itself
    still gets written normally, so nothing about the resulting row looks
    broken). Fixed by widening the text match to TECH_DATA_TEXT_SYNONYMS
    and adding the filename fallback as a safety net for wording this
    module hasn't confirmed real yet."""
    candidates = list(re.finditer(r'<a[^>]+href="([^"]+\.pdf)"[^>]*>([^<]*)</a>', html, re.I))

    for href, text in ((m.group(1), m.group(2)) for m in candidates):
        text_lower = text.lower()
        if any(synonym in text_lower for synonym in TECH_DATA_TEXT_SYNONYMS):
            return urljoin(base_url, href)

    for href in (m.group(1) for m in candidates):
        if _looks_like_tech_data_filename(href):
            return urljoin(base_url, href)

    return None


# <ul ... id="breadcrumbs">...</ul> -- confirmed real via curl this
# session against current, archived, and non-ball (bag/apparel) pages.
# Deliberately scoped to just this block before pulling itemprop="name"
# spans out of it -- an unscoped search over the whole page also matches
# the site's main nav menu items ("Company"/"Products"/"Events"/...),
# which happen to use the same itemprop="name" attribute and produced
# wrong results the first time this was tried against real HTML.
_BREADCRUMB_BLOCK_RE = re.compile(r'id="breadcrumbs">(.*?)</ul>', re.S)
_BREADCRUMB_ITEM_RE = re.compile(r'itemprop="name">([^<]+)</span>')


def parse_breadcrumb_trail(html: str) -> list:
    """Returns the ordered list of breadcrumb item names, e.g.
    ["Home", "Products", "Equipment", "Bowling Balls", "ALPHA CRUX"] for a
    current product, or ["Home", "Products", "Featured", "Bowling Balls
    Archive", "ABSOLUTE"] for an archived one -- both confirmed real via
    curl this session. Empty list if no breadcrumbs block is found."""
    m = _BREADCRUMB_BLOCK_RE.search(html)
    if not m:
        return []
    return [item.strip() for item in _BREADCRUMB_ITEM_RE.findall(m.group(1))]


def classify_product_status(html: str):
    """Returns "current", "retired" (this project's product_status enum
    value -- see db/migrations/001_init_schema.sql; the site's own UI
    calls these "archived" but the DB doesn't have that value), or None
    if this isn't a bowling ball product page at all (caller should skip
    it, not guess).

    Confirmed real via curl this session against all three brands' real
    archived pages, a real current page, and two real non-ball pages (a
    Roto Grip bag and hoodie) as controls -- the breadcrumb's category
    segment (second to last item, right before the product's own name) is
    a clean, reliable signal:
        current ball:  .../Equipment/Bowling Balls/<NAME>
        archived ball: .../Featured/Bowling Balls Archive/<NAME>
        bag:           .../Equipment/Bowling Bags/<NAME>
        apparel:       .../Merchandise/Apparel/<NAME>
    Only the first two return a real status; everything else returns None
    so _process_one() can skip it gracefully, same pattern as
    woocommerce_product_scraper.py's external_product sentinel."""
    trail = parse_breadcrumb_trail(html)
    if len(trail) < 2:
        return None
    category = trail[-2]
    if category == "Bowling Balls":
        return "current"
    if category == "Bowling Balls Archive":
        return "retired"
    return None


def parse_product_page(html: str, url: str) -> dict:
    spec = parse_spec_fields(html)
    coverstock = parse_coverstock(spec.get("coverstock"))

    return {
        "url": url,
        "status": classify_product_status(html),
        "name": parse_product_name(html),
        "sku_code": parse_sku_code(html),
        "brand_name": spec.get("brand"),
        "line": spec.get("line"),
        "core_name": spec.get("weight block") or spec.get("core"),
        "coverstock_name": spec.get("coverstock"),
        "coverstock_material": coverstock["coverstock_material"],
        "coverstock_type": coverstock["coverstock_type"],
        "core_type": parse_core_type(spec.get("symmetry")),
        "factory_finish": spec.get("finish"),
        "color": spec.get("color"),
        "release_date_raw": spec.get("release date"),
        "release_date": parse_release_date(spec.get("release date")),
        # The single weight/RG/Diff/PSA this page's raw HTML actually
        # shows -- used as a cross-check against the Tech Data PDF's full
        # table, not stored as the primary SKU source (see
        # cross_check_html_vs_pdf below). Always None for archived
        # products (they have no Weight/RG/Differential fields at all --
        # see classify_product_status's docstring), which is fine:
        # cross_check_html_vs_pdf already treats a missing html_weight_lbs
        # as "nothing to cross-check" rather than an error.
        "html_weight_lbs": int(_to_float(spec.get("weight"))) if spec.get("weight") else None,
        "html_rg": _to_float(spec.get("radius of gyration")),
        "html_differential": _to_float(spec.get("differential")),
        "tech_data_pdf_url": parse_tech_data_pdf_url(html, url),
        "main_image_url": parse_main_image_url(html, url),
        "description": parse_description(html),
    }


WEIGHT_TOKEN_RE = re.compile(r"^\s*\d{1,2}\s*lbs?\.?\s*$", re.I)


def _skus_from_table(table: list) -> list:
    """Pure logic for turning one pdfplumber-extracted table into a list
    of per-weight SKU dicts -- split out from parse_tech_data_pdf() so it
    can be tested directly against synthetic tables without needing to
    generate actual PDF bytes.

    Originally built against ONE real PDF (Storm Alpha Crux) and assumed
    every Tech Data PDF used that same shape. This deploy's first live
    smoke test (see COMMERCEBUILD_SCOPING.md) proved that wrong -- three
    real, confirmed, genuinely different shapes exist across this single
    catalog:

    1. Alpha Crux / Storm Ion Pro Solid: ONE row, 4 columns in a fixed
       [weight, rg, diff, psa] order, each cell holding all 5 weights'
       values newline-joined ("16 lb\\n15 lb\\n...").
    2. Roto Grip Gremlin: a real HEADER row ("WEIGHT"/"RG"/"DIFF"/"PSA")
       followed by 5 separate DATA rows, one row per weight, each cell a
       single un-joined value ("16 lbs.", "2.50", etc.) -- the more
       conventional "long" table shape.
    3. Storm Phaze II: same "one row, newline-joined" shape as #1, but
       with an extra blank leading column (`[None, weight, rg, diff]`
       instead of `[weight, rg, diff, psa]`) shifting every field over by
       one index, AND no PSA column at all for this product. The old
       fixed-index version silently read DIFF values into the PSA slot
       here instead of catching the shift.

    Rather than hardcoding three separate branches (fragile against a
    still-undiscovered fourth shape), this locates the weight column by
    CONTENT -- the one whose cell(s), split on newline, match
    WEIGHT_TOKEN_RE -- rather than assuming it's always column 0. Whether
    that column holds one token (case 2: "long" mode, one SKU per row) or
    multiple newline-joined tokens (cases 1/3: "wide" mode, one row holds
    every SKU) determines how the remaining columns get read. In wide
    mode, the remaining non-blank columns are taken in their original
    left-to-right order as [rg, diff, psa] -- correctly dropping Phaze
    II's blank leading column and correctly leaving psa=None when a
    product's PDF (like Phaze II's) doesn't report one, instead of
    mis-assigning a real diff value into the psa slot.

    A row/table where no column's tokens all look like weight values
    (e.g. Gremlin's own header row, or a stray "DESIGN INTENT:" row
    pdfplumber sometimes captures as part of the same table -- both
    confirmed real) is skipped rather than guessed at."""
    if not table:
        return []

    # Wide mode: find the first row containing a column whose SINGLE cell
    # holds multiple newline-joined weight tokens -- that row alone is
    # the whole table's data.
    for row in table:
        for idx, cell in enumerate(row):
            tokens = [t.strip() for t in (cell or "").split("\n") if t.strip()]
            if len(tokens) > 1 and all(WEIGHT_TOKEN_RE.match(t) for t in tokens):
                other_cols = [
                    (row[i] or "").split("\n")
                    for i in range(len(row))
                    if i != idx and (row[i] or "").strip()
                ]
                if not all(len(c) == len(tokens) for c in other_cols):
                    logger.warning(
                        "Tech Data PDF wide-format table: weight column has %d values but "
                        "other columns don't all match (%s) -- skipping, not guessing",
                        len(tokens), [len(c) for c in other_cols],
                    )
                    return []
                skus = []
                for i, w in enumerate(tokens):
                    weight_match = re.search(r"(\d{1,2})", w)
                    if not weight_match:
                        continue
                    values = [c[i].strip() for c in other_cols]
                    skus.append({
                        "weight_lbs": int(weight_match.group(1)),
                        "rg": _to_float(values[0]) if len(values) > 0 else None,
                        "differential": _to_float(values[1]) if len(values) > 1 else None,
                        "mass_bias": None,
                        "psa": _to_float(values[2]) if len(values) > 2 else None,
                    })
                return skus

    # Long mode: one row per weight, each row's weight cell a single token.
    skus = []
    weight_col_idx = None
    for row in table:
        for idx, cell in enumerate(row):
            token = (cell or "").strip()
            if token and WEIGHT_TOKEN_RE.match(token):
                weight_col_idx = idx
                break
        if weight_col_idx is not None:
            break

    if weight_col_idx is None:
        return []

    for row in table:
        if weight_col_idx >= len(row):
            continue
        token = (row[weight_col_idx] or "").strip()
        weight_match = re.search(r"(\d{1,2})", token) if WEIGHT_TOKEN_RE.match(token) else None
        if not weight_match:
            continue  # header row or unrelated row (e.g. "DESIGN INTENT:") -- skip, don't guess
        other_values = [
            (row[i] or "").strip()
            for i in range(len(row))
            if i != weight_col_idx and (row[i] or "").strip()
        ]
        skus.append({
            "weight_lbs": int(weight_match.group(1)),
            "rg": _to_float(other_values[0]) if len(other_values) > 0 else None,
            "differential": _to_float(other_values[1]) if len(other_values) > 1 else None,
            "mass_bias": None,
            "psa": _to_float(other_values[2]) if len(other_values) > 2 else None,
        })
    return skus


_TEXT_ROW_RE = re.compile(
    r"^(\d{1,2})\s+(-?\d*\.\d+|-?\d+)\s+(-?\d*\.\d+|-?\d+)(?:\s+(-?\d*\.\d+|-?\d+))?\s*$",
    re.M,
)


def _skus_from_text(text: str) -> list:
    """Fallback for Tech Data PDFs where the per-weight data has real,
    extractable text but pdfplumber's line-based table detection can't
    cleanly separate it into columns/cells -- confirmed real on Storm
    Lightning Storm Clear's PDF, where the whole product-details block
    AND the weight/RG/DIFF table both landed in one giant free-text table
    cell, with the actual data lines interleaved with unrelated
    contact-info lines (phone/email/website) sitting at the same vertical
    position on the page:
        "LBS RG DIFF\\n16 2.68 0.006\\n800-369-4402\\n15 2.69 0.006\\n
         tech@stormbowling.com\\n14 2.69 0.006\\n..."
    No column structure to key off of here at all, so this scans
    page.extract_text()'s raw output line by line instead, matching only
    lines shaped exactly like "<weight> <rg> <diff> [psa]". Confirmed
    this correctly skips the header line ("LBS RG DIFF", no leading
    digit) and the interleaved junk lines -- "800-369-4402" doesn't match
    because a 1-2 digit run isn't immediately followed by whitespace."""
    skus = []
    for m in _TEXT_ROW_RE.finditer(text):
        weight, rg, diff, psa = m.groups()
        skus.append({
            "weight_lbs": int(weight),
            "rg": _to_float(rg),
            "differential": _to_float(diff),
            "mass_bias": None,
            "psa": _to_float(psa) if psa else None,
        })
    return skus


def parse_tech_data_pdf(pdf_bytes: bytes) -> list:
    """Parses the per-weight spec table out of a Tech Data PDF -- see
    _skus_from_table()'s docstring for the real, confirmed table shapes
    this is built against.

    Real finding this deploy's first live smoke test: 900 Global's Viking
    Conquest Tech Data PDF (and, it turns out, some Storm PDFs too --
    Equinox Solid confirmed the same way) is genuinely image-based --
    confirmed via pdfplumber (0 chars, embedded images tiling the full
    page, 0 lines/rects) -- not a shape this function could ever parse
    regardless of table-detection logic, since there's no real text layer
    to read. OCR could theoretically recover it but that's real added
    scope (Tesseract in Lambda, reliability on numeric tables) not taken
    on here. Logs a distinct warning for this case specifically so it's
    never confused with "found a table but the shape didn't match" in the
    logs -- they need different fixes.

    Follow-up REAL INCIDENT, later session: this exact gap turned out to
    be Al's "900 Global balls are missing their skus and because of that
    pricing and other things that depend on that" report -- this
    function returning [] here means upsert_product's SKU-insert loop
    writes zero product_skus rows, and product_skus.weight_lbs is what
    price_checker's weight-match keys off of. OCR is still not
    implemented (same real-added-scope reasoning above still holds), but
    _process_one now falls back to _html_fallback_skus (a single
    HTML-sourced weight/RG/differential) whenever this function returns
    empty, so at least one real SKU row exists instead of zero -- see
    that function's own docstring and upsert_product's for the full fix.

    Second real finding, same smoke test: Storm Lightning Storm Clear has
    real, non-image text, but pdfplumber's table detection couldn't
    cleanly separate the per-weight data into columns at all -- see
    _skus_from_text()'s docstring. Falls back to that raw-text scan
    whenever table-based extraction finds real text but no usable rows,
    rather than giving up."""
    import pdfplumber

    skus = []
    total_chars = 0
    full_text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            total_chars += len(page.chars)
            for table in page.extract_tables():
                skus.extend(_skus_from_table(table))
            page_text = page.extract_text()
            if page_text:
                full_text_parts.append(page_text)

    if not skus and total_chars == 0:
        logger.warning(
            "Tech Data PDF has no extractable text (likely image-based/scanned) -- "
            "OCR would be required, not implemented, no per-weight data will be stored"
        )
        return skus

    if not skus:
        skus = _skus_from_text("\n".join(full_text_parts))
        if not skus:
            logger.warning(
                "Tech Data PDF has real text but no per-weight data could be found via "
                "table or raw-text parsing -- skipping, not guessing"
            )

    return skus


def cross_check_html_vs_pdf(parsed_page: dict, pdf_skus: list, tolerance: float = DEFAULT_TOLERANCE) -> list:
    """Compares the one weight/RG/Diff the product page's raw HTML shows
    against the matching weight row in the Tech Data PDF's full table.
    Real bug this project has hit twice already this deploy (pdf_parser,
    bowwwl_cross_check): DB-sourced values come back as decimal.Decimal
    via psycopg2, HTML/PDF-parsed values are plain float -- both operands
    are coerced to float here from the start, before either value could
    ever reach a DB round-trip, specifically to not repeat that bug a
    third time in a fourth module."""
    if parsed_page.get("html_weight_lbs") is None:
        return []

    match = next((s for s in pdf_skus if s["weight_lbs"] == parsed_page["html_weight_lbs"]), None)
    if match is None:
        return []

    mismatches = []
    for field, html_key, pdf_key in (
        ("rg", "html_rg", "rg"),
        ("differential", "html_differential", "differential"),
    ):
        html_value = parsed_page.get(html_key)
        pdf_value = match.get(pdf_key)
        if html_value is None or pdf_value is None:
            continue
        if abs(float(html_value) - float(pdf_value)) > tolerance:
            mismatches.append({
                "field_name": f"{field}_{parsed_page['html_weight_lbs']}lb",
                "current_value": str(html_value),
                "proposed_value": str(pdf_value),
                "reason": f"commercebuild html-vs-pdf: {field} at {parsed_page['html_weight_lbs']}lb disagrees (html={html_value}, pdf={pdf_value}, tolerance={tolerance})",
            })
    return mismatches


def _html_fallback_skus(parsed_page: dict) -> list:
    """REAL INCIDENT: Al reported "900 Global balls are missing their
    skus and because of that pricing and other things that depend on
    that." Confirmed root cause via a live fetch of a real 900 Global
    product (Viking): parse_tech_data_pdf_url correctly finds the Tech
    Data PDF link (this brand's Downloads section already uses the
    "Tech Data" wording verbatim), but parse_tech_data_pdf's own
    docstring already documented, from this deploy's first live smoke
    test, that 900 Global's Tech Data PDFs are frequently genuinely
    image-based (scanned/rasterized, zero real text layer) -- not a
    table-shape gap _skus_from_table could ever be widened to cover,
    since there's no text to read at all. pdf_skus then comes back
    empty, and upsert_product's `for sku in pdf_skus:` loop simply
    inserts zero product_skus rows -- exactly the downstream effect Al
    is seeing, since product_skus.weight_lbs is what price_checker's
    variant/weight matching (see that module's check_bigcommerce_
    sources) keys off of to attach cost/stock data to a specific SKU.

    Real OCR (Tesseract in Lambda) is still out of scope here, same as
    parse_tech_data_pdf's own docstring already concluded -- genuinely
    more work (reliability on numeric tables, a new Lambda layer) than
    this fix. Instead: parse_product_page already parses ONE real
    weight/RG/differential straight off the page's own visible HTML
    (html_weight_lbs/html_rg/html_differential -- previously used only
    for cross_check_html_vs_pdf's mismatch detection, see above). When
    the PDF path recovers nothing at all, that single HTML-sourced
    value is far better than zero SKUs: it's one real, confirmed weight
    (whichever the page defaults to showing) instead of the full
    per-weight table, but it's enough for price_checker's weight-match
    to actually find a row to attach to, unblocking the pricing gap Al
    flagged specifically. Tagged source='html' (not 'pdf') in the
    product_skus row this produces -- spec_source already has this
    value (see migration 001, and woocommerce_product_scraper/
    shopify_product_scraper/netsuite_product_scraper/product_scraper,
    which all use 'html' as their SKUs' only source already) -- so
    admin/reporting can tell a single html-sourced estimate apart from
    a full pdf-sourced table at a glance, same "flag, don't guess"
    spirit as review_queue elsewhere in this module.

    Returns [] (not a guess) when the page itself has no html_weight_lbs
    either -- e.g. an archived product's page, which per this module's
    own docstring has no Weight/RG/Differential fields at all."""
    if parsed_page.get("html_weight_lbs") is None:
        return []
    return [{
        "weight_lbs": parsed_page["html_weight_lbs"],
        "rg": parsed_page.get("html_rg"),
        "differential": parsed_page.get("html_differential"),
        "mass_bias": None,
        "source": "html",
    }]


# ---------------------------------------------------------------------
# Lambda handler + DB write. Same split as every other scraper in this
# project: pure parsing above (tested against reconstructed fixtures
# built from real confirmed field values -- see
# tests/test_commercebuild_product_scraper.py), mechanical I/O below,
# deferred-imported so the parsing tests don't need requests/psycopg2/
# boto3/pdfplumber installed to run.
# ---------------------------------------------------------------------

import json
import os


def fetch_page(url: str, timeout: int = 30) -> str:
    import requests

    resp = requests.get(url, headers={"User-Agent": "bowling-scraper/1.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def fetch_pdf(url: str, timeout: int = 30) -> bytes:
    import requests

    resp = requests.get(url, headers={"User-Agent": "bowling-scraper/1.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.content


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


def get_or_create_core_id(conn, brand_id: str, core_name, core_type=None):
    """Same helper as product_scraper.get_or_create_core_id -- duplicated
    rather than shared, same reasoning as publish_messages elsewhere in
    this project (each Lambda here is its own independent CodeUri package).
    This platform is one of the few that actually parses a real core_type
    (parse_core_type, from the "Symmetry" spec field) rather than always
    passing None -- see migration 007 for why the cores table exists."""
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


def _normalize_coverstock_name(name):
    """Same helper as product_scraper._normalize_coverstock_name --
    duplicated rather than shared, same reasoning as get_or_create_core_id
    above. Strips TM/R/C marks and collapses whitespace before a
    coverstock_name is used as the coverstocks table's lookup/create key
    -- Al directly reported real duplicate coverstocks rows where the
    exact same formulation shows up with a trailing (TM)/(R)/(C) symbol
    on some scrapes/products and not others (a manufacturer page
    inconsistency, not a scraper bug). Only the coverstocks table's
    canonical name is normalized this way -- the raw, as-scraped text
    still goes into products.coverstock_name completely unchanged."""
    if not name:
        return None
    cleaned = re.sub(r"[™®©]", "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def get_or_create_coverstock_id(conn, brand_id: str, coverstock_name, material=None, cs_type=None):
    """Same helper as product_scraper.get_or_create_coverstock_id --
    duplicated rather than shared, same reasoning as get_or_create_core_id
    above. See migration 008 for why this table exists, and
    _normalize_coverstock_name above for why the lookup key isn't just
    coverstock_name verbatim."""
    normalized_name = _normalize_coverstock_name(coverstock_name)
    if not normalized_name:
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
            (brand_id, normalized_name, material, cs_type),
        )
        return cur.fetchone()[0]


# --------------------------------------------------------------------
# Ball motion plotter: estimate-on-scrape (migrations 011/012) --
# duplicated from public_api/service.py's estimate_oil_motion rather
# than shared (each Lambda here is its own independent deployment
# package). MUST stay in sync with public_api's copy. See public_api/
# service.py's module-level comment above its own estimate_oil_motion
# for the full reasoning behind every constant here, and migration 012's
# header comment for why this hook exists at all -- Al's direct ask:
# "estimate on scrape if not set".
# --------------------------------------------------------------------

OIL_BASE_BY_MATERIAL = {
    "polyester_plastic": 2,
    "urethane": 6,
    "reactive_resin": 10,
}
OIL_ADJUST_BY_TYPE = {
    "pearl": -3,
    "hybrid": 0,
    "solid": 0,
}
OIL_PARTICLE_BONUS = 2

MOTION_BASE_BY_CORE_TYPE = {
    "symmetric": 4,
    "asymmetric": 8,
}
MOTION_BASE_UNKNOWN_CORE = 6
MOTION_DIFF_MIDPOINT = 0.02
MOTION_DIFF_SCALE = 0.045
MOTION_DIFF_WEIGHT = 8
MOTION_ADJUST_BY_COVERSTOCK_TYPE = {
    "pearl": 2,
    "solid": 1,
    "hybrid": 0,
}

OIL_MIN, OIL_MAX = 1, 16
MOTION_MIN, MOTION_MAX = 1, 18


def _clamp_oil_motion(value: float, low: int, high: int) -> int:
    return max(low, min(high, round(value)))


def estimate_oil_motion(core_type: str = None, coverstock_type: str = None,
                         coverstock_material: str = None, has_particle: bool = False,
                         differential: float = None) -> dict:
    """Identical logic to public_api.service.estimate_oil_motion -- see
    that module for the full reasoning. Duplicated, not imported."""
    oil = OIL_BASE_BY_MATERIAL.get(coverstock_material, (OIL_MIN + OIL_MAX) / 2)
    oil += OIL_ADJUST_BY_TYPE.get(coverstock_type, 0)
    if has_particle:
        oil += OIL_PARTICLE_BONUS
    oil = _clamp_oil_motion(oil, OIL_MIN, OIL_MAX)

    motion = MOTION_BASE_BY_CORE_TYPE.get(core_type, MOTION_BASE_UNKNOWN_CORE)
    if differential is not None:
        motion += ((float(differential) - MOTION_DIFF_MIDPOINT) / MOTION_DIFF_SCALE) * MOTION_DIFF_WEIGHT
    motion += MOTION_ADJUST_BY_COVERSTOCK_TYPE.get(coverstock_type, 0)
    motion = _clamp_oil_motion(motion, MOTION_MIN, MOTION_MAX)

    return {"oil": oil, "motion": motion}


def _reference_differential(skus: list):
    """Picks the differential of the SKU that best represents this
    product overall, straight off the just-parsed skus list -- normally
    the Tech Data PDF's table, or _html_fallback_skus' single estimated
    row when the PDF path recovered nothing (see upsert_product's
    docstring). Same 15lb-preferred convention as public_api._reference_
    sku."""
    usable = [s for s in skus if s.get("differential") is not None and s.get("weight_lbs") is not None]
    if not usable:
        return None
    for sku in usable:
        if sku["weight_lbs"] == 15:
            return sku["differential"]
    return min(usable, key=lambda s: abs(s["weight_lbs"] - 15))["differential"]


def upsert_product(conn, brand_id: str, parsed: dict, skus: list, mismatches: list) -> dict:
    """Insert/update the products row, its product_skus rows, and a
    single product_images row for the main image. Returns
    {"product_id": ..., "pending_image_jobs": [...]}, same shape as the
    other scrapers' upsert_product.

    `skus`: normally the Tech Data PDF's parsed table (each dict has no
    "source" key, defaulting to 'pdf' below). REAL INCIDENT (Al: "900
    Global balls are missing their skus...pricing and other things
    depend on that") -- when the PDF path recovers nothing at all
    (confirmed real for 900 Global: many of that brand's Tech Data PDFs
    are genuinely image-based/scanned, see parse_tech_data_pdf's own
    docstring), the caller (_process_one) passes _html_fallback_skus'
    single-SKU list instead, each dict explicitly tagged
    "source": "html". Reading source per-dict (falling back to 'pdf'
    when absent) rather than hardcoding one literal in the INSERT lets
    that distinction survive into the DB -- spec_source already has an
    'html' value for exactly this (migration 001; woocommerce_product_
    scraper/shopify_product_scraper/netsuite_product_scraper/
    product_scraper all already write 'html'-sourced SKUs as their only
    source), so admin/reporting can tell a single html-sourced estimate
    apart from a full pdf-sourced table at a glance -- same "flag,
    don't guess" spirit as review_queue below.

    Any html-vs-pdf mismatches found are written to review_queue rather
    than silently preferring one source over the other -- same "flag,
    don't guess" pattern as the rest of this project.

    status now comes from parsed["status"] (set by
    classify_product_status() -- "current" or "retired", this project's
    real product_status enum values) rather than being hardcoded, now that
    this scraper handles archived products too. Included in the ON
    CONFLICT update as well, so a product correctly flips to 'retired' if
    a later re-scrape finds it's moved to the archive listing."""
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
                coverstock_name, factory_finish, part_number, status, source_platform,
                release_date, description, core_id, coverstock_id
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'commercebuild', %s, %s, %s, %s)
            on conflict (url) do update set
                name = excluded.name,
                color = excluded.color,
                coverstock_material = excluded.coverstock_material,
                coverstock_type = excluded.coverstock_type,
                coverstock_name = excluded.coverstock_name,
                factory_finish = excluded.factory_finish,
                part_number = excluded.part_number,
                status = excluded.status,
                release_date = coalesce(excluded.release_date, products.release_date),
                description = coalesce(excluded.description, products.description),
                core_id = coalesce(excluded.core_id, products.core_id),
                coverstock_id = coalesce(excluded.coverstock_id, products.coverstock_id),
                updated_at = now()
            returning id
            """,
            (
                brand_id, parsed["name"], parsed["url"], parsed["color"],
                parsed["coverstock_material"], parsed["coverstock_type"],
                parsed["coverstock_name"], parsed["factory_finish"], parsed["sku_code"],
                parsed["status"], parsed["release_date"], parsed["description"], core_id, coverstock_id,
            ),
        )
        product_id = cur.fetchone()[0]

        for sku in skus:
            # source = excluded.source (not coalesced) -- unlike mass_bias,
            # a rescrape's source should always win outright: if this
            # weight was previously written by _html_fallback_skus
            # ('html', one estimated row) and a later rescrape finds the
            # Tech Data PDF newly readable (fixed upstream, or a future
            # OCR path), the real 'pdf' row should replace the fallback
            # estimate, not be silently kept alongside stale provenance.
            cur.execute(
                """
                insert into product_skus (product_id, weight_lbs, rg, differential, mass_bias, source)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (product_id, weight_lbs) do update set
                    rg = excluded.rg,
                    differential = excluded.differential,
                    mass_bias = coalesce(excluded.mass_bias, product_skus.mass_bias),
                    source = excluded.source,
                    updated_at = now()
                """,
                (product_id, sku["weight_lbs"], sku["rg"], sku["differential"], sku["mass_bias"], sku.get("source", "pdf")),
            )

        pending_image_jobs = []
        if parsed.get("main_image_url"):
            # display_order is REQUIRED -- see product_scraper/app.py's
            # (Brunswick) identical fix for the full incident: migration
            # 010 added this column NOT NULL with no database default, so
            # every scraper's INSERT here was silently aborting the whole
            # upsert_product transaction (NotNullViolation, not just this
            # row) until this was added. coalesce(max+1, 0) appends after
            # whatever's already there without disturbing any admin
            # reordering (only affects the brand-new-row INSERT branch,
            # never the ON CONFLICT DO UPDATE branch).
            cur.execute(
                """
                insert into product_images (product_id, image_type, source_url, display_order)
                values (%s, 'main', %s, coalesce((select max(display_order) + 1 from product_images where product_id = %s), 0))
                on conflict (product_id, source_url) do update set image_type = excluded.image_type
                returning id, stored_url
                """,
                (product_id, parsed["main_image_url"], product_id),
            )
            image_id, stored_url = cur.fetchone()
            if stored_url is None:
                pending_image_jobs.append({"product_image_id": str(image_id), "source_url": parsed["main_image_url"]})

        for m in mismatches:
            cur.execute(
                """
                insert into review_queue (product_id, field_name, current_value, proposed_value, source, reason)
                values (%s, %s, %s, %s, 'commercebuild_html_vs_pdf', %s)
                """,
                (product_id, m["field_name"], m["current_value"], m["proposed_value"], m["reason"]),
            )

        # Estimate-on-scrape plotter position (migrations 011/012) -- see
        # product_scraper/app.py's (Brunswick) identical hook for the full
        # reasoning. "where oil_rating is null" guards against clobbering
        # a chart match or an admin's manual correction on a rescrape.
        core_type = None
        if core_id is not None:
            cur.execute("select core_type from cores where id = %s", (core_id,))
            row = cur.fetchone()
            core_type = row[0] if row else None
        cur.execute("select has_particle from products where id = %s", (product_id,))
        has_particle = cur.fetchone()[0]
        estimate = estimate_oil_motion(
            core_type=core_type,
            coverstock_type=parsed["coverstock_type"],
            coverstock_material=parsed["coverstock_material"],
            has_particle=has_particle,
            differential=_reference_differential(skus),
        )
        cur.execute(
            "update products set oil_rating = %s, motion_rating = %s, oil_motion_source = 'estimated' "
            "where id = %s and oil_rating is null",
            (estimate["oil"], estimate["motion"], product_id),
        )

    conn.commit()
    return {"product_id": product_id, "pending_image_jobs": pending_image_jobs}


def build_image_process_messages(pending_image_jobs: list) -> list:
    return [json.dumps(job) for job in pending_image_jobs]


def publish_messages(sqs_client, queue_url: str, message_bodies: list) -> int:
    sent = 0
    for i in range(0, len(message_bodies), 10):
        chunk = message_bodies[i:i + 10]
        entries = [{"Id": str(idx), "MessageBody": body} for idx, body in enumerate(chunk)]
        sqs_client.send_message_batch(QueueUrl=queue_url, Entries=entries)
        sent += len(chunk)
    return sent


def _extract_jobs(event: dict) -> list:
    """Same shape-detection as every other scraper's handler: real SQS
    trigger ({"Records": [...]}) vs. direct/manual invocation
    ({"url": ..., "brand_id": ...})."""
    if "Records" in event:
        return [(json.loads(r["body"]), r["messageId"]) for r in event["Records"]]
    return [(event, None)]


def _process_one(job: dict) -> dict:
    url = job["url"]
    brand_id = job["brand_id"]

    html = fetch_page(url)
    parsed = parse_product_page(html, url)

    if parsed["status"] is None:
        logger.info(
            "Skipping %s -- breadcrumb category isn't Bowling Balls/Bowling Balls "
            "Archive (non-ball commercebuild product, e.g. bag/apparel/accessory "
            "that shares the same brand-prefixed URL shape as balls -- see "
            "classify_product_status's docstring)", url,
        )
        return {"product_id": None, "sku_count": 0, "image_jobs_published": 0, "skipped": "non_ball_product"}

    pdf_skus = []
    if parsed["tech_data_pdf_url"]:
        pdf_bytes = fetch_pdf(parsed["tech_data_pdf_url"])
        pdf_skus = parse_tech_data_pdf(pdf_bytes)
    else:
        logger.warning("No Tech Data PDF found for %s -- no per-weight SKU data will be stored", url)

    # Cross-check against the ORIGINAL pdf_skus (before any fallback
    # substitution below) -- cross_check_html_vs_pdf's whole purpose is
    # comparing the page's HTML value against the PDF's table, so if the
    # PDF path genuinely found nothing, there's nothing real to
    # cross-check (same as always -- returns [], unchanged behavior).
    mismatches = cross_check_html_vs_pdf(parsed, pdf_skus)

    # REAL INCIDENT (Al: "900 Global balls are missing their skus and
    # because of that pricing and other things that depend on that") --
    # see _html_fallback_skus' own docstring for the full root cause
    # (many 900 Global Tech Data PDFs are genuinely image-based, no text
    # layer, nothing table-parsing logic could ever recover). When the
    # PDF path found nothing at all, fall back to the one weight/RG/
    # differential the page's own HTML shows rather than writing zero
    # product_skus rows -- one real, source='html'-tagged SKU is enough
    # for price_checker's weight-match to find a row to attach cost/
    # stock data to, unblocking the downstream gap Al flagged, even
    # though it doesn't recover every weight the PDF table would have.
    skus_to_write = pdf_skus if pdf_skus else _html_fallback_skus(parsed)

    conn = get_db_connection()
    try:
        result = upsert_product(conn, brand_id, parsed, skus_to_write, mismatches)
    finally:
        conn.close()

    logger.info(
        "Scraped %s: %d SKUs from PDF, %d written (%s), %d mismatches, %d pending image jobs",
        url, len(pdf_skus), len(skus_to_write),
        "html fallback" if not pdf_skus and skus_to_write else "pdf",
        len(mismatches), len(result["pending_image_jobs"]),
    )

    image_queue_url = os.environ.get("IMAGE_PROCESS_QUEUE_URL")
    image_jobs_published = 0
    if result["pending_image_jobs"] and image_queue_url:
        import boto3

        sqs = boto3.client("sqs")
        messages = build_image_process_messages(result["pending_image_jobs"])
        image_jobs_published = publish_messages(sqs, image_queue_url, messages)

    return {
        "product_id": str(result["product_id"]),
        "sku_count": len(skus_to_write),
        "mismatch_count": len(mismatches),
        "image_jobs_published": image_jobs_published,
    }


def handler(event, context):
    """SQS-triggered from CommercebuildProductScrapeQueue (populated by
    CommercebuildUrlDiscoveryFunction) -- also accepts direct/manual
    invocation with {"url": "...", "brand_id": "..."}. Same partial-batch
    -failure handling as every other scraper here."""
    jobs = _extract_jobs(event)

    results = []
    batch_item_failures = []
    for job, message_id in jobs:
        try:
            results.append(_process_one(job))
        except Exception:
            logger.exception("Failed to process commercebuild product job: %r", job)
            if message_id is not None:
                batch_item_failures.append({"itemIdentifier": message_id})
            else:
                raise

    response = {"statusCode": 200, "body": json.dumps({"results": results})}
    if "Records" in event:
        response["batchItemFailures"] = batch_item_failures
    return response
