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
    """Return the first <table> where at least `min_matches` row-label cells
    (case-insensitive) are found in `known_labels`."""
    for table in soup.find_all("table"):
        matches = 0
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            label = _clean(cells[0].get_text()).lower()
            if label in known_labels:
                matches += 1
        if matches >= min_matches:
            return table
    return None


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
    material/type combinations."""
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


def upsert_product(conn, brand_id: str, parsed: dict) -> dict:
    """Insert or update the products row and its product_skus/product_images
    rows for one scraped page. Returns
    {"product_id": ..., "pending_image_jobs": [{"product_image_id", "source_url"}, ...]}
    -- the latter is every product_images row (new or pre-existing) that
    still has stored_url = null, i.e. still needs the image pipeline to
    run on it, which is what the handler uses to fan out image-process
    jobs without a separate query.

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
        for image in parsed["images"]:
            # Changed from the original "on conflict do nothing" to "do
            # update" (re-setting image_type to its own value) purely so
            # this can RETURNING id/stored_url on every row, whether it was
            # just inserted or already existed -- needed to know which
            # rows still need an image-process job without a second query.
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


def _process_one(job: dict, sqs_client) -> dict:
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

    return {
        "product_id": str(product_id),
        "sku_count": len(parsed["skus"]),
        "pdf_jobs_published": pdf_jobs_published,
        "image_jobs_published": image_jobs_published,
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
                raise  # direct invocation with no batch to report against -- surface the error

    response = {"statusCode": 200, "body": json.dumps({"results": results})}
    if "Records" in event:
        response["batchItemFailures"] = batch_item_failures
    return response
