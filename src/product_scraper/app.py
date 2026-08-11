"""
HTML product scraper for the Craft CMS brand family (Brunswick, Radical, DV8
-- confirmed to share the same template/URL shape during architecture
research). Shopify-family brands (Hammer/Ebonite/Track/Powerhouse) don't need
this: pull /products/{handle}.json directly instead, per the architecture doc.

Deliberately does NOT select elements by CSS class or id. Instead this
matches tables and fields by their visible text content (row labels like
"Part Number", header cells matching a "<N> lb" pattern), which is also just
a more resilient strategy in general: marketing sites rebuild their front end
far more often than they change field labels.

**Verified against real raw markup this session** (update: an earlier
session's research tooling only ever returned a markdown-converted view of
these pages, never raw HTML, so this file's table-matching logic went
untested against real markup for a while -- that's now closed. Using Claude
in Chrome, this session issued a literal `fetch()` from within a live browser
tab against both https://brunswickbowling.com/products/balls/current/crown-78u
and .../retired/defender and parsed the actual HTTP response body with
DOMParser -- not the JS-rendered DOM, the literal bytes `requests.get()` would
receive in production). Two real, previously-undetected bugs were found and
fixed as a direct result:

1. `parse_release_date()` only accepted "Month YYYY" (e.g. "December 2025").
   Crown 78U's real spec table row is "Release Date: December 11, 2025" --
   day-precision. The old format-only assumption came from a summary in the
   architecture doc, not the literal page value; the fixture actually had the
   real day-precision string all along, but no test checked
   `parse_product_page()`'s `release_date` field end-to-end against it, so
   this silently produced `None` in production. Now also tries "Month D,
   YYYY".
2. `parse_resources()` matched PDF resource type (Info Sheet / Ball Talker /
   Flip Card) by the `<a>` tag's own visible text. Real markup: every one of
   these links' own text is the generic word "Download" -- the actual
   per-resource label lives in a sibling heading inside the link's immediate
   wrapping container, confirmed identical on both real pages checked (each
   such container holds exactly one PDF link, so there's no cross-
   attribution risk). This meant `info_sheet_url` (what the PDF parser step
   depends on for mass bias) was never actually being populated. Fixed via
   `_nearby_label_text()` below, which climbs a bounded number of ancestors
   looking for the first one whose text says more than the link's own --
   content-based, not tied to any specific tag/class, per this file's
   existing philosophy.

The table structure, weight-column header pattern, spec-table label/value
shape, and image filename convention (`<N>-<M>_lb_Core...callout`) were all
otherwise confirmed to match this file's existing assumptions exactly -- no
other real discrepancies found. See tests/fixtures/crown_78u.html and
defender.html's header comments for the full disclosure of what's now
directly real-verified vs. still a values-only reconstruction (the full raw
HTML response is ~325KB, mostly cookie-consent-widget markup and tracking
scripts unrelated to parsing, and repeatedly triggered this sandbox's
anti-exfiltration safeguard when an attempt was made to transfer it verbatim
for a byte-for-byte fixture -- so the fixtures remain reconstructions, just
now built from individually re-confirmed real values/structure rather than
the prior session's markdown-derived guesses).

Mass bias (ASY) is often only present on the PDF "Info Sheet", not this
page -- see resources["info_sheet_url"] in the return value, which is what
the (not-yet-built) PDF parser step should consume. When ASY does appear
inline in the spec table (observed on some retired-ball pages), it's parsed
here.
"""
import logging
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger()
logger.setLevel(logging.INFO)

WEIGHT_HEADER_RE = re.compile(r"(\d{1,2})\s*lb", re.IGNORECASE)
STATUS_FROM_URL_RE = re.compile(r"/products/balls/(current|retired)/")

# Known Spec Table row labels. Detection requires only a majority match
# (not all of these) since real pages are inconsistent about which fields
# they include -- e.g. a retired Defender page has no Release Date row,
# while a current Crown 78U page does.
SPEC_TABLE_LABELS = {
    "level", "part number", "color", "core", "coverstock", "cover type",
    "finish", "weights", "rg", "diff", "asy", "mb", "warranty", "release date",
}

# Cover Type -> (coverstock_material, coverstock_type) per the schema. Some
# manufacturer values are compound ("Solid Reactive"), some are bare
# ("Urethane" with no type given) -- matched by substring rather than exact
# string so variants aren't silently dropped.
COVERSTOCK_MATERIAL_KEYWORDS = [
    ("urethane", "urethane"),
    ("polyester", "polyester_plastic"),
    ("reactive", "reactive_resin"),
]
COVERSTOCK_TYPE_KEYWORDS = [
    ("solid", "solid"),
    ("pearl", "pearl"),
    ("hybrid", "hybrid"),
]


def fetch_page(url: str, timeout: int = 30) -> str:
    """Fetch raw HTML. Kept separate from parsing so tests can feed real
    fixture HTML without a network call."""
    import requests

    resp = requests.get(url, headers={"User-Agent": "bowling-scraper/1.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _find_table_by_row_labels(soup: BeautifulSoup, known_labels: set, min_matches: int = 3):
    """Return the <table> with the MOST row-label cells (case-insensitive)
    found in `known_labels`, provided it clears `min_matches` -- not just
    the first table to reach that threshold.

    Real, confirmed bug this fixes (Al, 2026-08-10: "we are also missing a
    bunch of cores from the brunswick brand also their coverstocks. the
    combats are one of them"): SPEC_TABLE_LABELS includes "rg"/"diff"/
    "asy"/"mb" so the single-value fallback case (a page with no separate
    per-weight breakdown, just one RG/DIFF/ASY row inline in the real spec
    table -- see defender.html) gets found correctly. But on an
    asymmetric-core ball whose separate Core Numbers table (the per-weight
    RG/DIFF/ASY breakdown -- see _find_core_numbers_table) happens to
    report all three of RG, DIFF, AND ASY as its own rows, THAT table also
    clears min_matches=3 on its own -- and since it always appears before
    the real spec table in document order on every Brunswick product page
    (confirmed via a live fetch of brunswickbowling.com/products/balls/
    current/combat-solid, and reproduced with a matching two-table HTML
    fixture), first-match-wins returned the Core Numbers table instead,
    leaving spec = {"rg":..., "diff":..., "asy":...} with no core/
    coverstock/color/etc. at all -- core_name and coverstock_name (and
    every other spec field) silently came back None.

    This didn't affect every product: crown_78u.html's Core Numbers table
    only has RG/DIFF rows (no inline mass bias there), 2 matches, under
    the threshold, so it was correctly skipped even before this fix. It's
    specifically asymmetric-core balls reporting mass bias per-weight --
    Combat's whole family among them -- that tripped this.

    Picking the table with the most matches instead is robust to this: the
    real spec table has up to 10 possible matching labels (level, part
    number, color, core, coverstock, cover type, finish, weights,
    warranty, release date) against the Core Numbers table's max of 4 (rg,
    diff, asy, mb), so it wins outright whenever both tables are present.
    Ties keep the first table found (document order), same as the old
    behavior when there's no competing table at all."""
    best_table = None
    best_matches = 0
    for table in soup.find_all("table"):
        matches = 0
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            label = _clean(cells[0].get_text()).lower()
            if label in known_labels:
                matches += 1
        if matches >= min_matches and matches > best_matches:
            best_table = table
            best_matches = matches
    return best_table


def _find_core_numbers_table(soup: BeautifulSoup):
    """Return the first <table> whose header row cells look like weight
    labels ("16 lb", "15 lb", ...)."""
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if header_row is None:
            continue
        header_cells = [_clean(c.get_text()) for c in header_row.find_all(["th", "td"])]
        weight_cells = [c for c in header_cells if WEIGHT_HEADER_RE.search(c)]
        if len(weight_cells) >= 2:
            return table
    return None


def parse_spec_table(table) -> dict:
    """Spec table is label/value row pairs, e.g. <tr><td>Part Number</td><td>60-108363-93X</td></tr>."""
    spec = {}
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        label = _clean(cells[0].get_text()).lower()
        value = _clean(cells[1].get_text())
        if label in SPEC_TABLE_LABELS:
            spec[label] = value
    return spec


def parse_core_numbers_table(table) -> list:
    """Returns a list of {weight_lbs, rg, differential, mass_bias} dicts, one
    per weight column. Handles RG/DIFF and, if present, an ASY/MB row too."""
    header_row = table.find("tr")
    header_cells = [_clean(c.get_text()) for c in header_row.find_all(["th", "td"])]

    weights_by_column = {}
    for idx, cell in enumerate(header_cells):
        match = WEIGHT_HEADER_RE.search(cell)
        if match:
            weights_by_column[idx] = int(match.group(1))

    values_by_row_label = {}
    for row in table.find_all("tr")[1:]:
        cells = row.find_all(["th", "td"])
        if not cells:
            continue
        row_label = _clean(cells[0].get_text()).lower()
        values_by_row_label[row_label] = [_clean(c.get_text()) for c in cells]

    results = []
    for col_idx, weight in weights_by_column.items():
        def value_at(row_label):
            cells = values_by_row_label.get(row_label)
            if not cells or col_idx >= len(cells):
                return None
            v = cells[col_idx]
            return v if v else None

        rg = value_at("rg")
        diff = value_at("diff") or value_at("differential")
        mass_bias = value_at("asy") or value_at("mb")

        if rg is None and diff is None:
            continue

        results.append({
            "weight_lbs": weight,
            "rg": _to_float(rg),
            "differential": _to_float(diff),
            "mass_bias": _to_float(mass_bias),
        })

    return results


def _to_float(value):
    if value is None:
        return None
    match = re.search(r"[\d.]+", value)
    return float(match.group()) if match else None


def parse_coverstock(cover_type_value: str) -> dict:
    """Splits a raw "Cover Type" spec value into the material/type facets
    the schema uses. Deliberately returns type=None rather than guessing
    when the value doesn't specify one (e.g. bare "Urethane") -- per the
    architecture doc, graphic/spare balls in particular often genuinely
    don't have a disclosed type."""
    if not cover_type_value:
        return {"coverstock_material": None, "coverstock_type": None}

    lowered = cover_type_value.lower()

    material = None
    for keyword, value in COVERSTOCK_MATERIAL_KEYWORDS:
        if keyword in lowered:
            material = value
            break

    cs_type = None
    for keyword, value in COVERSTOCK_TYPE_KEYWORDS:
        if keyword in lowered:
            cs_type = value
            break

    return {"coverstock_material": material, "coverstock_type": cs_type}


def parse_release_date(release_date_raw: str):
    """Parses Brunswick's real "Release Date" spec row value into a date.
    Two confirmed real shapes: day-precision "Month D, YYYY" (e.g.
    "December 11, 2025" -- Crown 78U's actual live value, confirmed via a
    literal raw-HTTP fetch this session, see this module's docstring) and
    the day-less "Month YYYY" (e.g. "December 2025" -- from an earlier
    session's architecture-doc notes, which may have simply summarized
    the day-precision value rather than reflecting a genuinely different
    page format; kept since it costs nothing to still accept it and no
    live page has disproven it). Defaults to the 1st of the month only
    for the day-less shape. Accepts both full ("December") and
    abbreviated ("Dec") month names since which one any given page uses
    hasn't been exhaustively checked. Returns None rather than guessing
    for anything that doesn't match either shape -- e.g. a blank
    release_date_raw (some retired pages, like Defender, confirmed live
    this session to not have this field at all)."""
    if not release_date_raw:
        return None
    cleaned = release_date_raw.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def parse_weights_available(weights_value: str):
    """Parses "16-12 lbs." into (low, high) = (12, 16). Returns None if it
    doesn't match the expected "<high>-<low> lbs" shape rather than guessing."""
    if not weights_value:
        return None
    match = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})", weights_value)
    if not match:
        return None
    a, b = int(match.group(1)), int(match.group(2))
    return (min(a, b), max(a, b))


def _nearby_label_text(link, max_levels: int = 4) -> str:
    """Real markup, confirmed live on both Crown 78U's and Defender's
    actual pages this session: every PDF resource link's own visible text
    is just the generic word "Download" -- the real per-resource label
    ("Crown 78U Info Sheet", "Defender Ball Talker", ...) lives in a
    sibling heading inside the link's immediate wrapping container, not
    on the link itself. Climbs up to `max_levels` ancestors looking for
    the first one whose full text says more than the link's own text --
    in practice this is the link's immediate wrapping element, which real
    markup scopes to exactly one PDF resource at a time (confirmed: never
    more than one PDF link inside that container on either page checked).
    Bounded on purpose: climbing unboundedly risks eventually reaching an
    ancestor that also contains a *different* PDF resource's label,
    misattributing it. Falls back to the link's own text (e.g. plain
    "Download", which won't match any of the label keywords below and
    correctly lands the URL in the "other" bucket) if nothing more
    specific is found within the bound -- safe rather than wrong.

    get_text(separator=" ") is used rather than the no-argument default
    on purpose: this file's other get_text() calls only ever read a
    single cell's text (never spans a tag boundary), but this function
    concatenates a whole subtree's text, and production markup could be
    minified (no whitespace between adjacent tags) in a way this
    session's real-but-unminified page check wouldn't have caught --
    without an explicit separator, minified markup could run two
    adjacent words together across a tag boundary."""
    own_text = _clean(link.get_text(separator=" "))
    own_lower = own_text.lower()
    node = link
    for _ in range(max_levels):
        node = node.parent
        if node is None or getattr(node, "name", None) in (None, "[document]", "html", "body"):
            break
        text = _clean(node.get_text(separator=" "))
        if text and text.lower() != own_lower:
            return text
    return own_text


def parse_description(soup: BeautifulSoup) -> str:
    """Brunswick's marketing description text for this specific ball --
    e.g. Strategy's page has a real paragraph starting "Brunswick is
    excited to introduce Strategy, the newest addition to its Pro
    Performance lineup...". Confirmed live via Claude in Chrome on the
    Strategy product page: the text sits in a `div.u-hide` inside
    `.c-product-feature__info-body` -- visually hidden via CSS (behind a
    "read more" toggle, going by the class name), but present verbatim in
    the raw server HTML `requests.get()` receives, confirmed by issuing a
    literal `fetch()` of the page's own raw response body from inside a
    live tab and checking the description text was in it, not just the
    JS-rendered/hydrated DOM.

    This is a deliberate, narrow exception to this module's usual
    "match by visible text content, not CSS class" philosophy (see module
    docstring): unlike spec table rows, this content has no text label of
    its own to match against. `.u-hide` is a generic Tailwind-style
    utility class that could plausibly recur elsewhere on the page for
    unrelated reasons (e.g. mobile-only nav text), so this doesn't just
    grab the first match anywhere in the document -- it's scoped to
    descend from `.c-product-feature__info-body` (the container
    immediately following the H1) and requires a minimum length, since
    stray hidden utility text elsewhere tends to be short labels/buttons,
    not paragraph-length marketing copy. Returns None rather than raising
    if the page doesn't have one (e.g. a colorway variant of an existing
    ball, or the site's markup shifts) -- description is enrichment, not
    a required field."""
    for candidate in soup.select(".c-product-feature__info-body .u-hide"):
        text = _clean(candidate.get_text(separator=" "))
        if len(text) > 40:
            return text
    return None


def parse_resources(soup: BeautifulSoup, base_url: str) -> dict:
    """Captures PDF resource links, keyed by a normalized name. The Info
    Sheet is what the (not-yet-built) PDF parser step consumes for mass
    bias when it's not inline in the HTML spec table. Matches against
    _nearby_label_text() rather than the link's own text -- see that
    function's docstring and this module's docstring for why."""
    resources = {}
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not href.lower().endswith(".pdf"):
            continue
        label = _nearby_label_text(link).lower()
        url = urljoin(base_url, href)
        if "info sheet" in label:
            resources["info_sheet_url"] = url
        elif "ball talker" in label:
            resources["ball_talker_url"] = url
        elif "flip card" in label:
            resources["flip_card_url"] = url
        else:
            resources.setdefault("other", []).append(url)
    return resources


def _resolve_img_src(img, base_url: str):
    """Returns the best real URL for an <img> tag, or None if none can be
    resolved.

    Real bug, found via production CloudWatch logs: sections marked
    loading="lazy" (e.g. the "Performance Index" chart images) set `src` to
    an inline transparent SVG placeholder
    (data:image/svg+xml;charset=utf-8,...) and put the actual candidate
    URLs in `srcset` instead. Using `img["src"]` directly picked up that
    placeholder as source_url, which was then stored as-is and later failed
    in image_processor with `requests.exceptions.InvalidSchema: No
    connection adapters were found for 'data:image/svg+xml...'` -- `data:`
    isn't a scheme `requests` (or any HTTP fetch) can handle.

    Fix: prefer the highest-resolution candidate from `srcset` (format is
    "<url> <width>w, <url> <width>w, ..."); fall back to `src` only if
    there's no usable srcset, and never return a `data:` URI either way."""
    srcset = img.get("srcset")
    if srcset:
        candidates = []
        for part in srcset.split(","):
            part = part.strip()
            if not part:
                continue
            bits = part.split()
            url = bits[0]
            width = 0
            if len(bits) > 1 and bits[1].endswith("w"):
                try:
                    width = int(bits[1][:-1])
                except ValueError:
                    width = 0
            if not url.startswith("data:"):
                candidates.append((width, url))
        if candidates:
            candidates.sort(key=lambda c: c[0])
            return urljoin(base_url, candidates[-1][1])

    src = img.get("src")
    if src and not src.startswith("data:"):
        return urljoin(base_url, src)

    return None


def parse_images(soup: BeautifulSoup, base_url: str) -> list:
    """Main product image plus per-weight-range core callout images, matched
    by filename pattern rather than alt text -- alt text spells weights out
    in words ("sixteen to fourteen pound") inconsistently, filenames use the
    reliable "16-14" numeric pattern."""
    images = []
    seen_urls = set()

    for img in soup.find_all("img"):
        src = _resolve_img_src(img, base_url)
        if src is None:
            continue
        if src in seen_urls:
            continue
        seen_urls.add(src)

        alt = _clean(img.get("alt", ""))
        callout_match = re.search(r"(\d{1,2})-(\d{1,2})_lb_Core", src)

        if callout_match:
            images.append({
                "image_type": "core_callout",
                "weight_lbs_context_high": int(callout_match.group(1)),
                "weight_lbs_context_low": int(callout_match.group(2)),
                "source_url": src,
                "alt": alt,
            })
        elif not images:  # first non-callout image encountered = main product shot
            images.append({"image_type": "main", "source_url": src, "alt": alt})
        else:
            images.append({"image_type": "other", "source_url": src, "alt": alt})

    return images


def parse_product_page(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    status_match = STATUS_FROM_URL_RE.search(url)
    status = status_match.group(1) if status_match else None

    h1 = soup.find("h1")
    name = _clean(h1.get_text()) if h1 else None

    spec_table = _find_table_by_row_labels(soup, SPEC_TABLE_LABELS)
    spec = parse_spec_table(spec_table) if spec_table is not None else {}

    core_numbers_table = _find_core_numbers_table(soup)
    skus = parse_core_numbers_table(core_numbers_table) if core_numbers_table is not None else []

    # Per the architecture review: when there's no per-weight breakdown,
    # a single RG/DIFF/ASY value in the spec table is conventionally the
    # 15 lb ball.
    if not skus and ("rg" in spec or "diff" in spec):
        skus = [{
            "weight_lbs": 15,
            "rg": _to_float(spec.get("rg")),
            "differential": _to_float(spec.get("diff")),
            "mass_bias": _to_float(spec.get("asy") or spec.get("mb")),
        }]

    coverstock = parse_coverstock(spec.get("cover type"))

    return {
        "url": url,
        "status": status,
        "name": name,
        "color": spec.get("color"),
        "core_name": spec.get("core"),
        "coverstock_name": spec.get("coverstock"),
        "coverstock_material": coverstock["coverstock_material"],
        "coverstock_type": coverstock["coverstock_type"],
        "factory_finish": spec.get("finish"),
        "part_number": spec.get("part number"),
        "weights_available": parse_weights_available(spec.get("weights")),
        "release_date_raw": spec.get("release date"),  # kept as text too -- release_date below is the parsed version, raw stays for anything parse_release_date rejects
        "release_date": parse_release_date(spec.get("release date")),
        "description": parse_description(soup),
        "skus": skus,
        "resources": parse_resources(soup, url),
        "images": parse_images(soup, url),
    }


# ---------------------------------------------------------------------
# Lambda handler + DB write. Kept below the pure parsing functions above
# on purpose -- those are the part worth testing carefully (see
# tests/test_product_scraper.py); this part is comparatively mechanical
# upsert logic, deferred-imported so the parsing tests don't need
# psycopg2/boto3 installed to run.
#
# Orchestration: this function is now SQS-triggered from
# PRODUCT_SCRAPE_QUEUE_URL's queue (see UrlDiscoveryFunction, which
# publishes there) rather than only manually invoked. After a successful
# scrape it fans out two more jobs of its own: a PDF-parse job (when
# info_sheet_url was found) and an image-process job per product_images
# row that still needs mirroring (stored_url is null) -- see
# build_pdf_parse_message / build_image_process_messages below.
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


def get_or_create_core_id(conn, brand_id: str, core_name, core_type=None):
    """Looks up or inserts a cores row for this brand+core_name, returns
    its id (or None if core_name wasn't parsed off this page). Idempotent
    across repeated scrapes of different products that share one physical
    core -- e.g. DV8's Collision core, used by six differently-named balls
    (Intense Collision, Severe Collision, Wicked Collision, Violent
    Collision, Brutal Collision, and Collision itself): each scrape
    resolves to the same cores row via the (brand_id, name) unique
    constraint rather than creating a duplicate.

    core_type is only ever filled in when the existing row doesn't already
    have one (coalesce, never overwrite) -- this project has no observed
    case of the same core name legitimately reporting two different
    core_types, so this errs toward not overwriting rather than building
    review_queue machinery for a situation that hasn't come up. See
    migration 007 for why this table exists at all (it's a rename/repurpose
    of the previously-unused ball_families table, not a new addition)."""
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
    """Strips TM/R/C marks and collapses whitespace before using a
    coverstock_name as the coverstocks table's lookup/create key -- Al
    directly reported real duplicate coverstocks rows where the exact
    same formulation shows up with a trailing (TM)/(R)/(C) symbol on some
    scrapes/products and not others (a manufacturer page inconsistency,
    not a scraper bug): "R2S Solid Reactive" and "R2S™ Solid Reactive"
    were creating two coverstocks rows for what's really one coverstock.
    Only the coverstocks table's canonical name is normalized this way --
    the raw, as-scraped text still goes into products.coverstock_name
    completely unchanged (see migration 008's own comment on why that
    column stays raw)."""
    if not name:
        return None
    cleaned = re.sub(r"[™®©]", "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def get_or_create_coverstock_id(conn, brand_id: str, coverstock_name, material=None, cs_type=None):
    """Same shape as get_or_create_core_id above, for coverstocks instead
    of cores -- see migration 008 for why this table exists. Al's ask,
    directly parallel to cores: coverstock_name is a shared, brand-scoped
    marketing name (e.g. "HK22 - Savvy Hook Hybrid") that multiple
    differently-named products can share, invisible as a many-to-one
    relationship while it only lived as a repeated free-text column on
    products. material/type are coalesced the same way core_type is (never
    overwrite an existing value) -- this project has no observed case of
    the same coverstock name legitimately reporting two different
    material/type combinations.

    The lookup/create key is _normalize_coverstock_name(coverstock_name),
    not the raw value -- see that function's docstring for why (real TM-
    symbol inconsistency Al found in the data)."""
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
# package -- see publish_messages' docstring below for the same
# reasoning applied elsewhere in this file). MUST stay in sync with
# public_api's copy: an estimate written here at scrape time and one
# public_api would compute for the same inputs must always agree. See
# public_api/service.py's module-level comment above its own
# estimate_oil_motion for the full reasoning behind every constant here,
# and migration 012's header comment for why this hook exists at all --
# Al's direct ask: "estimate on scrape if not set".
# --------------------------------------------------------------------

OIL_BASE_BY_MATERIAL = {
    "polyester_plastic": 2,
    "urethane": 5,
    "reactive_resin": 10,
}
OIL_ADJUST_BY_TYPE = {
    "pearl": -3,
    "hybrid": 0,
    "solid": 3,
}
OIL_PARTICLE_BONUS = 2

MOTION_BASE_BY_CORE_TYPE = {
    "symmetric": 7,
    "asymmetric": 12,
}
MOTION_BASE_UNKNOWN_CORE = 9
MOTION_DIFF_MIDPOINT = 0.02
MOTION_DIFF_SCALE = 0.045
MOTION_DIFF_WEIGHT = 6
MOTION_ADJUST_BY_COVERSTOCK_TYPE = {
    "pearl": 1,
    "solid": -1,
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
    product overall, straight off the just-parsed skus list (no DB
    round-trip needed -- upsert_product already has it in hand). Same
    15lb-preferred convention as public_api._reference_sku (see
    001_init_schema.sql's own stated convention): prefer the real 15lb
    row, else the row closest to 15lb, else None if nothing here has a
    usable weight+differential pair."""
    usable = [s for s in skus if s.get("differential") is not None and s.get("weight_lbs") is not None]
    if not usable:
        return None
    for sku in usable:
        if sku["weight_lbs"] == 15:
            return sku["differential"]
    return min(usable, key=lambda s: abs(s["weight_lbs"] - 15))["differential"]


def upsert_product(conn, brand_id: str, parsed: dict) -> dict:
    """Insert or update the products row and its product_skus/product_images
    rows for one scraped page. Returns
    {"product_id": ..., "pending_image_jobs": [...], "stale_image_rows": [...]}
    -- pending_image_jobs is every product_images row (new or pre-existing)
    that still has stored_url = null, i.e. still needs the image pipeline
    to run on it, which is what the handler uses to fan out image-process
    jobs without a separate query. stale_image_rows is every product_images
    row that existed before this scrape but whose source_url isn't in the
    current parse -- i.e. a photo the page no longer has -- deleted here
    and returned (rather than just deleted) so _process_one can also clean
    up any S3 objects already mirrored for those rows via
    delete_orphaned_image_objects. Ported from netsuite_product_scraper's
    identical fix (see DEPLOY_RUNBOOK.md's MOTIV image-cleanup writeup):
    without this, a plain upsert never removes a row for a photo that's no
    longer on the page, so a rescrape only ever adds to the image set,
    never actually replaces it.

    Mismatches between a re-scrape and the stored value aren't silently
    overwritten for SKU fields sourced from html when a prior value came
    from pdf -- that's exactly what review_queue exists for (see the
    architecture doc's "mismatched" definition). This function writes the
    html-sourced fields directly since that's the higher-confidence, more
    complete source for RG/DIFF; reconciling against a pdf-sourced or
    bowwwl-sourced value is the PDF parser step's job, not this one's.
    """
    weights_range = None
    if parsed["weights_available"]:
        low, high = parsed["weights_available"]
        weights_range = f"[{low},{high}]"

    # This platform doesn't expose a dedicated symmetric/asymmetric field
    # anywhere on the page (see parse_product_page) -- core_type stays None
    # here, same as it always has. Only core_name is new.
    core_id = get_or_create_core_id(conn, brand_id, parsed.get("core_name"))
    coverstock_id = get_or_create_coverstock_id(
        conn, brand_id, parsed.get("coverstock_name"),
        parsed.get("coverstock_material"), parsed.get("coverstock_type"),
    )

    # discontinued_detected_at logic (see migration 003's comments for the
    # full reasoning): on INSERT, set to now() if the product is already
    # retired the first time we ever see it. On UPDATE, the CASE
    # expression sets it only on a genuine current->retired transition
    # (comparing the existing row's status to the incoming one), leaves it
    # alone on a repeat 'retired' scrape, and clears it if status ever
    # reverts to 'current'.
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
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::int4range, %s, 'craft_cms', %s, %s,
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
                -- coalesce, not overwrite: a page-parse hiccup that misses
                -- the hidden u-hide description (see parse_description's
                -- docstring) shouldn't null out a previously-good value,
                -- same reasoning as release_date above.
                description = coalesce(excluded.description, products.description),
                -- same coalesce reasoning: a scrape that doesn't find a
                -- "Core" spec value (page layout hiccup, etc.) shouldn't
                -- null out a core_id a previous scrape already resolved.
                core_id = coalesce(excluded.core_id, products.core_id),
                -- same reasoning again, for coverstock_id (migration 008).
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
                weights_range, parsed["status"], parsed["release_date"], parsed["description"],
                parsed["status"], core_id, coverstock_id,
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
        current_source_urls = set()
        for image in parsed["images"]:
            # Changed from the original "on conflict do nothing" to "do
            # update" (re-setting image_type to its own value) purely so
            # this can RETURNING id/stored_url on every row, whether it was
            # just inserted or already existed -- needed to know which
            # rows still need an image-process job without a second query.
            #
            # display_order is REQUIRED here -- real, confirmed production
            # incident (2026-08-11): migration 010 added product_images.
            # display_order as NOT NULL with no database default (by
            # design -- see that migration's own comment, "no scraper
            # touches any of these three fields", which turned out to be
            # wrong: every scraper still has to INSERT a value for a
            # NOT NULL column with no default, even one it never reads).
            # Without this, every new-image insert raised
            # psycopg2.errors.NotNullViolation, which aborted this entire
            # upsert_product transaction (this INSERT runs before the
            # final commit) -- not just the image row. Found while
            # investigating why Combat's core_id still wasn't set even
            # after fixing _find_table_by_row_labels: the parser was
            # already correct, but the whole upsert was silently rolling
            # back on every single product, catalog-wide, across all five
            # scrapers (this same insert shape exists in each one).
            # coalesce(max(display_order)+1 over this product's existing
            # rows, 0) keeps new images appended after whatever's already
            # there, matching migration 010's 0-based per-product
            # ordering -- an admin's prior manual reordering (see
            # admin_api.reorder_product_images) is never disturbed, since
            # this only ever assigns a value to a brand-new row (the ON
            # CONFLICT DO UPDATE branch for an existing row doesn't touch
            # display_order at all).
            current_source_urls.add(image["source_url"])
            cur.execute(
                """
                insert into product_images (product_id, image_type, source_url, display_order)
                values (%s, %s, %s, coalesce((select max(display_order) + 1 from product_images where product_id = %s), 0))
                on conflict (product_id, source_url) do update set image_type = excluded.image_type
                returning id, stored_url
                """,
                (product_id, image["image_type"], image["source_url"], product_id),
            )
            image_id, stored_url = cur.fetchone()
            if stored_url is None:
                pending_image_jobs.append({"product_image_id": str(image_id), "source_url": image["source_url"]})

        # Real cleanup step, ported from netsuite_product_scraper (MOTIV) --
        # see that module's upsert_product docstring for the full incident
        # this closes. A plain upsert on its own never REMOVES a row -- it
        # only inserts new source_urls or updates ones still present. Al
        # confirmed Brunswick has the same gap MOTIV had: a rescrape after
        # a page's photos changed (or after a scraper fix that stops
        # collecting a wrong photo) just adds whatever the current parse
        # found alongside whatever wrong/stale rows were already sitting
        # there, never removing them. This makes a rescrape genuinely
        # REPLACE the image set with whatever the current parse actually
        # found, not just extend it.
        #
        # `returning id, stored_url` -- same reasoning as netsuite's
        # version: a bare DELETE would leave any already-uploaded S3
        # objects for these rows orphaned forever, since nothing else in
        # this codebase ever revisits an already-processed image. Returning
        # the deleted rows here (rather than a bare DELETE) is what lets
        # _process_one below actually clean those up via
        # delete_orphaned_image_objects().
        if current_source_urls:
            cur.execute(
                """
                delete from product_images where product_id = %s and source_url <> all(%s)
                returning id, stored_url
                """,
                (product_id, list(current_source_urls)),
            )
        else:
            # parsed["images"] came back empty this time -- every existing
            # row for this product is stale by definition.
            cur.execute(
                "delete from product_images where product_id = %s returning id, stored_url",
                (product_id,),
            )
        stale_image_rows = [{"id": row[0], "stored_url": row[1]} for row in cur.fetchall()]

    # Estimate-on-scrape plotter position (migrations 011/012) -- only
    # ever touches a product with NO plotter position at all yet ("where
    # oil_rating is null" guards against clobbering a chart match from
    # scripts/backfill_plotter_chart_positions.py or an admin's manual
    # correction on a rescrape). core_type isn't in `parsed` for this
    # platform (see the comment above get_or_create_core_id's call site
    # a few lines up -- Brunswick's own pages don't expose a dedicated
    # symmetric/asymmetric field), so it's read back from the cores row
    # itself, which may have picked one up from a different platform's
    # scrape of the same shared core or an admin correction.
    with conn.cursor() as cur:
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
            differential=_reference_differential(parsed["skus"]),
        )
        cur.execute(
            "update products set oil_rating = %s, motion_rating = %s, oil_motion_source = 'estimated' "
            "where id = %s and oil_rating is null",
            (estimate["oil"], estimate["motion"], product_id),
        )

    conn.commit()
    return {"product_id": product_id, "pending_image_jobs": pending_image_jobs, "stale_image_rows": stale_image_rows}


def build_pdf_parse_message(product_id: str, info_sheet_url: str) -> str:
    """Pure function, no SQS/boto3 dependency -- unit-testable on its own."""
    return json.dumps({"product_id": str(product_id), "info_sheet_url": info_sheet_url})


def build_image_process_messages(pending_image_jobs: list) -> list:
    return [json.dumps(job) for job in pending_image_jobs]


def publish_messages(sqs_client, queue_url: str, message_bodies: list) -> int:
    """Sends message_bodies to queue_url via SendMessageBatch, chunked to
    SQS's 10-message-per-call limit. Returns the count sent. Duplicated
    from url_discovery/app.py rather than shared -- each Lambda here is
    its own independent deployment package (CodeUri), and introducing a
    shared Lambda Layer for one seven-line helper isn't worth the added
    packaging complexity yet."""
    sent = 0
    for i in range(0, len(message_bodies), 10):
        chunk = message_bodies[i:i + 10]
        entries = [{"Id": str(idx), "MessageBody": body} for idx, body in enumerate(chunk)]
        sqs_client.send_message_batch(QueueUrl=queue_url, Entries=entries)
        sent += len(chunk)
    return sent


def delete_orphaned_image_objects(s3_client, bucket: str, stale_image_rows: list) -> int:
    """Ported from netsuite_product_scraper/app.py -- see that module's own
    docstring for the full story (Al asked directly whether a rescrape
    also cleans up the S3 objects image_processor may have already
    uploaded for a now-stale product_images row; without this, the answer
    was no, and those objects would be orphaned forever).

    Only bothers with rows that actually have a stored_url (`stored_url is
    not None` -- see the `stale_image_rows` filtering in _process_one): a
    row still awaiting image_processor never had anything uploaded for it,
    so there's nothing in S3 to clean up.

    Mirrors image_processor.upload_variants' real key convention
    (`product-images/<product_image_id>/<size_name>.png`) rather than
    importing it -- separate Lambda deployment packages, same "own the
    whole package" duplication convention as publish_messages above.
    Lists each id's objects (via `list_objects_v2`, paginated) and deletes
    whatever comes back, rather than hard-coding image_processor's current
    SIZE_PRESETS name set (thumbnail/catalog/detail) here too -- correct
    even if that preset list changes later without this module needing to
    track it.

    Returns the total object count actually deleted, for logging."""
    s3_paginator = s3_client.get_paginator("list_objects_v2")
    deleted = 0
    for row in stale_image_rows:
        if row["stored_url"] is None:
            continue

        prefix = f"product-images/{row['id']}/"
        keys = []
        for page in s3_paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        if not keys:
            continue

        s3_client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys]})
        deleted += len(keys)

    return deleted


def _extract_jobs(event: dict) -> list:
    """Supports two invocation shapes: a real SQS trigger
    ({"Records": [{"body": "<json>", "messageId": "..."}, ...]}) and a
    direct/manual invocation ({"url": "...", "brand_id": "..."}). Returns
    a list of (job_dict, message_id_or_None) pairs so the handler can
    report per-message failures back to SQS (message_id is None for a
    direct invocation, where there's no batch to report against)."""
    if "Records" in event:
        return [(json.loads(r["body"]), r["messageId"]) for r in event["Records"]]
    return [(event, None)]


def _process_one(job: dict, sqs_client, s3_client=None) -> dict:
    """Scrapes and upserts one product page, then fans out follow-up jobs.
    Raised exceptions propagate to the caller (handler), which decides how
    to report the failure -- kept separate so handler can catch per-job
    rather than letting one bad URL fail an entire SQS batch."""
    url = job["url"]
    brand_id = job["brand_id"]

    logger.info("Scraping %s", url)
    html = fetch_page(url)
    parsed = parse_product_page(html, url)

    conn = get_db_connection()
    try:
        result = upsert_product(conn, brand_id, parsed)
    finally:
        conn.close()

    product_id = result["product_id"]
    logger.info("Upserted product %s (%d SKUs)", product_id, len(parsed["skus"]))

    pdf_queue_url = os.environ.get("PDF_PARSE_QUEUE_URL")
    info_sheet_url = parsed["resources"].get("info_sheet_url")
    pdf_jobs_published = 0
    if info_sheet_url and pdf_queue_url:
        message = build_pdf_parse_message(product_id, info_sheet_url)
        pdf_jobs_published = publish_messages(sqs_client, pdf_queue_url, [message])

    image_queue_url = os.environ.get("IMAGE_PROCESS_QUEUE_URL")
    image_jobs_published = 0
    if result["pending_image_jobs"] and image_queue_url:
        messages = build_image_process_messages(result["pending_image_jobs"])
        image_jobs_published = publish_messages(sqs_client, image_queue_url, messages)

    # Ported from netsuite_product_scraper/app.py's _process_one -- see
    # upsert_product's stale_image_rows and delete_orphaned_image_objects's
    # own docstring for the full story. image_bucket is optional (soft-
    # fails, same convention as image_queue_url above) -- a deployment
    # that hasn't set IMAGE_BUCKET on this function yet just skips cleanup
    # and logs why, rather than erroring; the DB-side fix (the delete
    # itself, in upsert_product) already works either way.
    orphaned_objects_deleted = 0
    stale_with_stored_url = [r for r in result["stale_image_rows"] if r["stored_url"] is not None]
    image_bucket = os.environ.get("IMAGE_BUCKET")
    if stale_with_stored_url:
        if s3_client is not None and image_bucket:
            orphaned_objects_deleted = delete_orphaned_image_objects(s3_client, image_bucket, stale_with_stored_url)
            logger.info(
                "Deleted %d orphaned S3 object(s) for %d stale image row(s)",
                orphaned_objects_deleted, len(stale_with_stored_url),
            )
        else:
            logger.warning(
                "%d stale product_images row(s) had a stored_url but IMAGE_BUCKET/s3_client "
                "isn't configured -- their S3 objects are orphaned (not cleaned up this run)",
                len(stale_with_stored_url),
            )

    return {
        "product_id": str(product_id),
        "sku_count": len(parsed["skus"]),
        "pdf_jobs_published": pdf_jobs_published,
        "image_jobs_published": image_jobs_published,
        "orphaned_objects_deleted": orphaned_objects_deleted,
    }


def handler(event, context):
    """Handles both an SQS-triggered batch (ProductScrapeQueue, populated by
    UrlDiscoveryFunction) and a direct/manual invocation with
    {"url": "...", "brand_id": "..."}. When SQS-triggered, uses Lambda's
    partial batch response feature (ReportBatchItemFailures, set on the
    event source mapping in template.yaml) so one bad URL doesn't cause the
    whole batch to be retried -- only the failed message(s) go back on the
    queue."""
    jobs = _extract_jobs(event)

    sqs_client = None
    if any(os.environ.get(k) for k in ("PDF_PARSE_QUEUE_URL", "IMAGE_PROCESS_QUEUE_URL")):
        import boto3

        sqs_client = boto3.client("sqs")

    # s3_client for delete_orphaned_image_objects (see _process_one) --
    # built once here and reused across every job in this batch, same
    # "build the client once per invocation" pattern as sqs_client above.
    # Ported from netsuite_product_scraper/app.py's handler.
    s3_client = None
    if os.environ.get("IMAGE_BUCKET"):
        import boto3

        s3_client = boto3.client("s3")

    results = []
    batch_item_failures = []
    for job, message_id in jobs:
        try:
            results.append(_process_one(job, sqs_client, s3_client))
        except Exception:
            logger.exception("Failed to scrape/upsert job: %r", job)
            if message_id is not None:
                batch_item_failures.append({"itemIdentifier": message_id})
            else:
                raise  # direct invocation with no batch to report against -- surface the error

    response = {"statusCode": 200, "body": json.dumps({"results": results})}
    if "Records" in event:
        response["batchItemFailures"] = batch_item_failures
    return response
