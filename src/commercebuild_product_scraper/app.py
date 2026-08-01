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


def parse_tech_data_pdf_url(html: str, base_url: str):
    """Finds the "Tech Data" PDF link in the Downloads section by LINK
    TEXT content ("tech data", case-insensitive), not by filename pattern
    -- confirmed real filenames vary wildly ("Alpha Crux Tech Data
    Final.pdf" vs "Tech Doc_HP3_GREMLIN.pdf") but the link text itself
    reliably contains "Tech Data" on both products checked. Same
    content-over-structure matching philosophy as the Craft-CMS scraper's
    _nearby_label_text/parse_resources."""
    for m in re.finditer(r'<a[^>]+href="([^"]+\.pdf)"[^>]*>([^<]*)</a>', html, re.I):
        href, text = m.group(1), m.group(2)
        if "tech data" in text.lower():
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


def upsert_product(conn, brand_id: str, parsed: dict, pdf_skus: list, mismatches: list) -> dict:
    """Insert/update the products row, its product_skus rows (sourced
    from the Tech Data PDF -- source='pdf', the schema's existing
    spec_source enum already supports this, no migration needed), and a
    single product_images row for the main image. Returns
    {"product_id": ..., "pending_image_jobs": [...]}, same shape as the
    other scrapers' upsert_product.

    Any html-vs-pdf mismatches found are written to review_queue rather
    than silently preferring one source over the other -- same "flag,
    don't guess" pattern as the rest of this project.

    status now comes from parsed["status"] (set by
    classify_product_status() -- "current" or "retired", this project's
    real product_status enum values) rather than being hardcoded, now that
    this scraper handles archived products too. Included in the ON
    CONFLICT update as well, so a product correctly flips to 'retired' if
    a later re-scrape finds it's moved to the archive listing."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into products (
                brand_id, name, url, color, coverstock_material, coverstock_type,
                coverstock_name, factory_finish, part_number, status, source_platform,
                release_date
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'commercebuild', %s)
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
                updated_at = now()
            returning id
            """,
            (
                brand_id, parsed["name"], parsed["url"], parsed["color"],
                parsed["coverstock_material"], parsed["coverstock_type"],
                parsed["coverstock_name"], parsed["factory_finish"], parsed["sku_code"],
                parsed["status"], parsed["release_date"],
            ),
        )
        product_id = cur.fetchone()[0]

        for sku in pdf_skus:
            cur.execute(
                """
                insert into product_skus (product_id, weight_lbs, rg, differential, mass_bias, source)
                values (%s, %s, %s, %s, %s, 'pdf')
                on conflict (product_id, weight_lbs) do update set
                    rg = excluded.rg,
                    differential = excluded.differential,
                    mass_bias = coalesce(excluded.mass_bias, product_skus.mass_bias),
                    updated_at = now()
                """,
                (product_id, sku["weight_lbs"], sku["rg"], sku["differential"], sku["mass_bias"]),
            )

        pending_image_jobs = []
        if parsed.get("main_image_url"):
            cur.execute(
                """
                insert into product_images (product_id, image_type, source_url)
                values (%s, 'main', %s)
                on conflict (product_id, source_url) do update set image_type = excluded.image_type
                returning id, stored_url
                """,
                (product_id, parsed["main_image_url"]),
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

    mismatches = cross_check_html_vs_pdf(parsed, pdf_skus)

    conn = get_db_connection()
    try:
        result = upsert_product(conn, brand_id, parsed, pdf_skus, mismatches)
    finally:
        conn.close()

    logger.info(
        "Scraped %s: %d SKUs from PDF, %d mismatches, %d pending image jobs",
        url, len(pdf_skus), len(mismatches), len(result["pending_image_jobs"]),
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
        "sku_count": len(pdf_skus),
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
