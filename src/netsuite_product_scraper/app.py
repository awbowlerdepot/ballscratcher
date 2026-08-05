"""
HTML product scraper for the NetSuite brand family (MOTIV Bowling to start
-- motivbowling.com).

Platform confirmation, done this session via a real Chrome browser (Claude
in Chrome), not a guess: a category-listing tile's href resolves to
"https://www.motivbowling.com/n_<18-digit-id>", and navigating it redirects
(confirmed via window.location after navigation) to a human-readable
canonical URL, e.g. "https://www.motivbowling.com/n_782172039773743965"
-> "https://www.motivbowling.com/products/balls/heavy-oil/jackal-onyx.html".
That /n_<id> permalink shape is a NetSuite SuiteCommerce signature. No
`<meta name="generator">` tag or window.NetSuite-style global was found
(checked directly this session), so this is inferred from the URL
convention, not a platform banner -- consistent with a heavily re-themed/
custom-templated SuiteCommerce storefront, not an off-the-shelf one.

Every field/markup structure this module parses was read directly off two
real, live product pages in a real browser this session (Sigma Tour Pearl
-- symmetric core -- and Jackal Onyx -- asymmetric core -- plus a spot
check of an "Ascend" colorway ball). Specifically NOT verified: whether
every ball on the site follows this exact markup, especially non-ball
products (bags/apparel/accessories) or older archived pages, since this
was only checked against a small sample of current balls.

FETCHING IS THE UNVERIFIED PART, NOT THE PARSING. Re-confirmed fresh this
session via `mcp__workspace__web_fetch`: motivbowling.com's HOME PAGE and
CATEGORY pages fetch fine and return real content, but this exact Sigma
Tour Pearl product page still returns a blank response to a plain
non-browser request, unchanged from an earlier session's finding.

**New this session: the pure "you just need a session cookie" theory
behind fetch_page's current approach was tested directly and partially
falsified.** From a live browser tab already on this exact product URL,
`fetch(location.href)` returned the full real page (54KB, contains
"Sigma Tour Pearl" and "RG") -- expected, since the browser has a real
session. But `fetch(location.href, {credentials: 'omit'})` -- explicitly
sending NO cookies at all -- returned the exact same full real content,
same length. That means a cookie-less request from a real browser
context still succeeds, so cookies alone don't explain why
`mcp__workspace__web_fetch`'s cookie-less request comes back blank.
Whatever the real differentiator is, it's something else a genuine
browser does that a bare non-browser HTTP client doesn't. Also checked
via `resp.headers.get('server')`: the only identifying response header
present is a bare "Apache" -- no Cloudflare/Akamai/Fastly/Sucuri/
Datadome/Imperva signature, so this doesn't look like it's sitting
behind a dedicated enterprise bot-management product (though an
unbranded Apache module, or something at the TLS layer that wouldn't
show up in HTTP headers at all, can't be ruled out this way).

Given that, `fetch_page()`'s realistic User-Agent/Accept/Accept-Language/
Referer headers (already implemented below) are more likely the
load-bearing part of this workaround than the session-cookie logic is --
the cookie-first visit may be unnecessary, or may matter for reasons
this test didn't isolate (e.g. only relevant to some other check, or a
red herring entirely). This is still not proof `fetch_page()` will work
in production: no way exists in this environment to test with a
genuinely bare `requests.get()` (no browser involved at all) to see
whether it's headers, TLS fingerprint, or something else entirely that's
the real gate. If it still comes back blank after a real deployment,
the next things to try, in order of how much this session's evidence
favors them: (1) double-check every header a real browser sends that
`fetch_page()` doesn't yet (sec-fetch-*, sec-ch-ua client hints, a
matching Referer chain) before assuming it's unfixable with headers
alone; (2) if that doesn't work, the gate is likely below the HTTP
layer (TLS fingerprint/JA3, HTTP/2-specific behavior) and no
`requests`-based approach will fix it -- fall back to a headless-browser
-based fetch instead.

Real, confirmed structural facts this module's parsing rests on:

1. Two catalog index pages exist (current vs. retired), not a URL path
   segment on the product page itself -- see netsuite_url_discovery's
   docstring. So status can't be inferred from the product URL the way
   Brunswick does; it's passed through the SQS job message instead (see
   the job shape in _extract_jobs below), populated by the discovery
   step. Defaults to "current" for a direct/manual invocation that omits
   it, since that's the far more common case to be testing against.
2. Header block: <div class="heading"> containing
   <h2 class="item-number"><span>MTVBSAPRL</span></h2> (maps to the
   schema's part_number column, same convention as Brunswick's Part
   Number),
   <div class="item-name-plus-release-date"><h1>NAME</h1>
     <span class="release-date">AVAILABLE 7/8/2026</span></div>,
   <div class="price"><span class="current-price">$224.99</span></div>.
   Price is captured in the parsed dict for completeness but not
   persisted -- the products table has no price column (checked
   db/migrations/001_init_schema.sql; no other manufacturer's scraper
   captures price either).
3. Colorway-in-name: a subset of balls (the "Designer Series"/entry-level
   lines -- Ascend, Aspire, seen in the category listing) put the colorway
   directly in the product name as "BASE NAME - COLOR/COLOR/COLOR", e.g.
   "Ascend - Green/Teal/Black" (confirmed on that ball's own page this
   session, not just the category tile). Regular performance-line balls
   (Sigma Tour Pearl, Jackal Onyx) have no " - " in their name and are a
   single fixed color per model with no separate "Color" spec field
   anywhere on the page. So color here is inferred by splitting the name
   on " - " rather than read from a dedicated field -- genuinely
   different from both other manufacturers, which each have (or don't
   populate) an explicit color spec value.
4. Two <table>s inside <section class="product-specifications">: the
   first is ball-motion metrics (Length/Backend/Hook/Flare Potential --
   not in the current schema, captured in the parsed dict as
   motion_metrics_raw but not persisted, same "captured but not
   persisted" treatment as price); the second has Weight Block (->
   core_name), Cover Stock (-> coverstock_name, and the source for
   coverstock_material/type via the same keyword-matching approach as
   Brunswick's single-field parse_coverstock, e.g. "Atomic Propulsion
   Pearl Reactive" contains both "Reactive" and "Pearl"), Finish (->
   factory_finish), and Weight Range (a plain comma-separated list of
   integers with NO "lb" suffix, e.g. "12, 13, 14, 15, 16" -- different
   token shape than either other manufacturer's weight field).
5. Per-weight RG/DIFF/mass-bias, in HTML, not a PDF (like SWAG, unlike
   Brunswick): <div class="product-specifications-by-weight"> contains a
   flickity carousel of <li class="slide"> items, one per weight, each
   with <h3 class="weight">16</h3> and heading/value span pairs. Confirmed
   real on both a symmetric ball (Sigma Tour Pearl: "Radius of Gyration" +
   "Max Differential" only, 5 weights) and an asymmetric one (Jackal Onyx:
   same two PLUS a third "Int. Differential" pair -- MOTIV's own name for
   what the schema calls mass_bias -- 5 weights, all captured this
   session). Flickity clones slides for its infinite-loop carousel
   behavior, so raw parsing can see duplicate weight values; dedupe by
   weight_lbs.
6. Downloads: <section class="product-downloads"><ul><li><a href="...">
   <span class="heading">PDF</span><span class="value">LABEL</span></a>
   </li>...</ul></section>. Confirmed real, but the LABEL set is NOT
   fixed across products -- Sigma Tour Pearl has "Sell Sheet" + "Shelf
   Talker"; Jackal Onyx has "Shelf Talker" + "Factory Finish Guide". So
   unlike Brunswick's fixed info_sheet/ball_talker/flip_card keys, this
   captures whatever labels are actually present, normalized to
   snake_case + "_url" keys, rather than assuming a fixed set.
7. Images: NOT <img> tags -- confirmed this session that
   `document.querySelectorAll('img')` inside <main> returns nothing for
   these pages. Real product photos are CSS background-image: url(...)
   inline styles, on <a> tags for the main gallery (3 real photos seen:
   front, side/back, and a third angle) and on a <div class="image"> for
   a "core cutaway" shot specifically -- served from a different path,
   "userfiles/filemanager-format/core-image/<id>", a transform of one of
   the main gallery's own image ids. Both patterns matched via regex
   against the raw HTML restricted to userfiles/filemanager paths (the
   site also has plenty of unrelated background-image icons elsewhere in
   the page under /assets/images/, deliberately excluded by requiring
   "userfiles/filemanager" in the path).

   Real bug found later via production DLQ investigation (two real
   products failed image processing with a 404 on
   ".../userfiles/filemanager-format/core-image/"): for products with no
   real core-cutaway photo, MOTIV's own template renders this exact same
   background-image style with an EMPTY path --
   `background-image: url(./userfiles/filemanager-format/core-image/)`,
   literally no id after the trailing slash -- once per weight-variant
   slide (3 real occurrences confirmed via curl against
   motivbowling.com/n_659670458713337742, a Japan-exclusive ball that
   plausibly never got a real core-cutaway photo taken). The old regex
   happily matched this empty-path style since it only required
   "userfiles/filemanager" to appear somewhere inside the parens, storing
   a real-looking but fundamentally fileless URL that then 404s in
   image_processor forever, no matter how many times it's retried (the
   bug is in what got stored, not in any transient fetch failure). Same
   "recognize and skip known non-image placeholder shapes" spirit as
   Brunswick's data: URI lazy-load fix and commercebuild's
   ajax-loader.gif/coming_soon.jpg skip -- see parse_images()'s
   docstring below for the fix (skip any captured URL with nothing after
   its final "/").
"""
import logging
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger()
logger.setLevel(logging.INFO)

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

WEIGHT_RANGE_TOKEN_RE = re.compile(r"\d{1,2}")
NUMBER_RE = re.compile(r"-?(?:\d+\.?\d*|\.\d+)")

# Restricted to userfiles/filemanager paths specifically -- see module
# docstring point 7 for why (excludes unrelated /assets/images/ icons that
# also use inline background-image styling elsewhere on the page).
IMAGE_RE = re.compile(r'background-image:\s*url\(([^)]*userfiles/filemanager[^)]*)\)')


def fetch_page(url: str, timeout: int = 30) -> str:
    """Session-cookie-first fetch -- see module docstring's large caveat.
    Visits the homepage first to acquire whatever session cookie NetSuite
    issues, then reuses it (via a requests.Session, which also handles the
    redirect from a /n_<id> URL to its canonical slug automatically) for
    the actual product-page request. UNVERIFIED end to end -- this sandbox
    cannot make an outbound request to motivbowling.com to confirm it
    actually returns real content rather than another blank response."""
    import requests

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    session = requests.Session()
    session.headers.update(headers)
    session.get("https://www.motivbowling.com/", timeout=timeout)  # acquire session cookie

    resp = session.get(url, headers={"Referer": "https://www.motivbowling.com/"}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_name_and_colorway(raw_name: str):
    """Splits "Ascend - Green/Teal/Black" into ("Ascend", "Green/Teal/Black").
    Returns (raw_name, None) unchanged when there's no " - " separator --
    the common case for regular performance-line balls. See module
    docstring point 3."""
    if not raw_name:
        return raw_name, None
    if " - " not in raw_name:
        return raw_name, None
    base, colorway = raw_name.split(" - ", 1)
    return base.strip(), colorway.strip()


def parse_release_date(release_date_raw: str):
    """MOTIV's format is genuinely different from Brunswick/SWAG's "Month
    YYYY" -- real confirmed values this session: "AVAILABLE 7/8/2026",
    "AVAILABLE 1/8/2025" (upcoming/recent releases, "AVAILABLE " prefix),
    and a bare "10/22/2025" with no prefix (seen on Raptor Reign, whose
    release date is further in the past -- see the README's "Third
    manufacturer" section for the observation that the prefix's presence
    may itself be a secondary status signal, not relied on here). Strips
    an optional "AVAILABLE " prefix (case-insensitive) then parses
    M/D/YYYY -- full day precision, unlike Brunswick/SWAG's month-only
    format. Returns None for anything that doesn't match."""
    if not release_date_raw:
        return None
    cleaned = re.sub(r"^available\s+", "", release_date_raw.strip(), flags=re.IGNORECASE)
    try:
        return datetime.strptime(cleaned, "%m/%d/%Y").date()
    except ValueError:
        return None


def parse_header(soup: BeautifulSoup) -> dict:
    heading = soup.find("div", class_="heading")
    if heading is None:
        return {"item_number": None, "name": None, "release_date_raw": None, "price_raw": None}

    item_number_el = heading.find("h2", class_="item-number")
    item_number = _clean(item_number_el.get_text()) if item_number_el else None

    h1 = heading.find("h1")
    name = _clean(h1.get_text()) if h1 else None

    release_el = heading.find("span", class_="release-date")
    release_date_raw = _clean(release_el.get_text()) if release_el else None

    price_el = heading.find("span", class_="current-price")
    price_raw = _clean(price_el.get_text()) if price_el else None

    return {
        "item_number": item_number,
        "name": name,
        "release_date_raw": release_date_raw,
        "price_raw": price_raw,
    }


def parse_specifications(soup: BeautifulSoup) -> dict:
    """Reads both tables inside section.product-specifications. Matches by
    row label text (case-insensitive), same content-matching philosophy as
    the other two scrapers, rather than assuming table order/position."""
    section = soup.find("section", class_="product-specifications")
    spec = {}
    if section is None:
        return spec

    for table in section.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = _clean(cells[0].get_text()).lower()
            value = _clean(cells[1].get_text())
            spec[label] = value

    return spec


def parse_weight_slides(soup: BeautifulSoup) -> list:
    """Returns a list of {weight_lbs, rg, differential, mass_bias} dicts,
    one per distinct weight found in the product-specifications-by-weight
    carousel. See module docstring point 5 -- mass_bias (MOTIV calls it
    "Int. Differential") is only present for asymmetric balls; None for
    symmetric ones. Deduped by weight_lbs since flickity clones slides for
    its carousel loop."""
    container = soup.find("div", class_="product-specifications-by-weight")
    if container is None:
        return []

    seen_weights = set()
    results = []
    for li in container.find_all("li", class_="slide"):
        weight_el = li.find("h3", class_="weight")
        if weight_el is None:
            continue
        weight_text = _clean(weight_el.get_text())
        if not weight_text.isdigit():
            continue
        weight_lbs = int(weight_text)
        if weight_lbs in seen_weights:
            continue
        seen_weights.add(weight_lbs)

        headings = [_clean(s.get_text()).lower() for s in li.find_all("span", class_="heading")]
        values = [_clean(s.get_text()) for s in li.find_all("span", class_="value")]
        by_label = dict(zip(headings, values))

        rg = _to_float(by_label.get("radius of gyration"))
        differential = _to_float(by_label.get("max differential"))
        mass_bias = _to_float(by_label.get("int. differential"))

        if rg is None and differential is None:
            continue

        results.append({
            "weight_lbs": weight_lbs,
            "rg": rg,
            "differential": differential,
            "mass_bias": mass_bias,
        })

    return sorted(results, key=lambda s: s["weight_lbs"])


def _to_float(value):
    if not value:
        return None
    match = NUMBER_RE.search(value)
    return float(match.group()) if match else None


def parse_coverstock(cover_stock_value: str) -> dict:
    """Single-field keyword matching, same approach as Brunswick's
    parse_coverstock (unlike SWAG, which splits material/type across two
    fields) -- MOTIV's "Cover Stock" value (e.g. "Atomic Propulsion Pearl
    Reactive") carries both material and type keywords in one string."""
    if not cover_stock_value:
        return {"coverstock_material": None, "coverstock_type": None}

    lowered = cover_stock_value.lower()

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


def parse_weights_available(weight_range_value: str):
    """Parses "12, 13, 14, 15, 16" (no "lb" suffix, unlike either other
    manufacturer's weight field) into (low, high) = (12, 16)."""
    if not weight_range_value:
        return None
    weights = [int(w) for w in WEIGHT_RANGE_TOKEN_RE.findall(weight_range_value)]
    if not weights:
        return None
    return (min(weights), max(weights))


def parse_resources(soup: BeautifulSoup, base_url: str) -> dict:
    """Captures every Downloads link, keyed by its own label text
    (normalized), rather than a fixed set of expected labels -- see
    module docstring point 6 for why (the label set genuinely varies
    per product)."""
    resources = {}
    section = soup.find("section", class_="product-downloads")
    if section is None:
        return resources

    for link in section.find_all("a", href=True):
        value_el = link.find("span", class_="value")
        label = _clean(value_el.get_text()) if value_el else _clean(link.get_text())
        if not label:
            continue
        key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") + "_url"
        resources[key] = urljoin(base_url, link["href"])

    return resources


def parse_images(html: str, base_url: str) -> list:
    """Regex-based (not BeautifulSoup) since the image is CSS
    background-image on the style attribute, not an <img> src -- see
    module docstring point 7.

    Real bug found via production DLQ investigation: products with no
    real core-cutaway photo get a background-image style with an EMPTY
    path (confirmed real: `url(./userfiles/filemanager-format/core-image/)`,
    nothing after the trailing slash) -- this still matches IMAGE_RE (it
    only requires "userfiles/filemanager" to appear inside the parens)
    but isn't a real image, and 404s forever in image_processor no matter
    how many times it's retried. Skipped by requiring at least one
    non-slash character after the URL's final "/" -- same
    skip-a-known-non-image-shape spirit as Brunswick's data: URI check."""
    images = []
    seen = set()
    for raw_src in IMAGE_RE.findall(html):
        src = urljoin(base_url, raw_src)
        if src in seen:
            continue
        seen.add(src)

        if src.endswith("/"):
            continue

        if "filemanager-format/core-image" in src:
            image_type = "core_callout"
        elif not images:
            image_type = "main"
        else:
            image_type = "other"

        images.append({"image_type": image_type, "source_url": src})

    return images


def parse_description(soup: BeautifulSoup) -> str:
    """MOTIV's marketing description, e.g. the Sigma Tour Pearl page opens
    with "Some sequels are worth the wait. Back in 2011, the Sigma Tour
    became one of those balls bowlers never stopped talking about...".
    Confirmed live via Claude in Chrome: sits in a "wysiwyg" div inside the
    product order form (`section.product form.order-form div.wysiwyg`) --
    and, same verification as the other three scrapers' parse_description,
    confirmed present in the raw server HTML MOTIV's fetch_page() actually
    receives (checked via a literal fetch() of the page's own response body
    from a live tab), not something only the client-side render produces,
    so this doesn't need any JS-execution workaround beyond what
    fetch_page() already does to get past MOTIV's bot-blocking (see this
    module's docstring)."""
    el = soup.select_one("section.product form.order-form div.wysiwyg")
    if el is None:
        return None
    text = _clean(el.get_text(separator=" "))
    return text or None


def parse_product_page(html: str, url: str, status: str = "current") -> dict:
    soup = BeautifulSoup(html, "lxml")

    header = parse_header(soup)
    name, colorway = parse_name_and_colorway(header["name"])

    spec = parse_specifications(soup)
    coverstock = parse_coverstock(spec.get("cover stock"))

    return {
        "url": url,
        "status": status,
        "name": name,
        "color": colorway,
        "part_number": header["item_number"],
        "price_raw": header["price_raw"],
        "release_date_raw": header["release_date_raw"],
        "release_date": parse_release_date(header["release_date_raw"]),
        "core_name": spec.get("weight block"),
        "coverstock_name": spec.get("cover stock"),
        "coverstock_material": coverstock["coverstock_material"],
        "coverstock_type": coverstock["coverstock_type"],
        "factory_finish": spec.get("finish"),
        "weights_available": parse_weights_available(spec.get("weight range")),
        "motion_metrics_raw": {
            k: v for k, v in spec.items()
            if k in ("length", "backend", "hook", "flare potential")
        },
        "skus": parse_weight_slides(soup),
        "resources": parse_resources(soup, url),
        "images": parse_images(html, url),
        "description": parse_description(soup),
    }


# ---------------------------------------------------------------------
# Lambda handler + DB write. Same split/orchestration pattern as
# product_scraper/app.py and woocommerce_product_scraper/app.py -- pure
# parsing above (tested), mechanical DB/SQS below (deferred-imported).
#
# Now SQS-triggered from NetsuiteProductScrapeQueue (populated by
# NetsuiteUrlDiscoveryFunction) -- see template.yaml. Fans out to the
# SAME ImageProcessQueue/ImageProcessorFunction the other two platforms
# use (platform-agnostic job shape, no reason to duplicate it a third
# time). Still no PdfParseQueue fan-out -- MOTIV doesn't need the PDF
# step for its core data either (RG/DIFF/mass-bias all come from HTML,
# see module docstring point 5).
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
    """Same helper as product_scraper.get_or_create_core_id -- duplicated
    rather than shared, same reasoning as publish_messages elsewhere in
    this project (each Lambda here is its own independent CodeUri package).
    MOTIV's spec fields don't expose a dedicated symmetric/asymmetric field
    (see this module's docstring), so core_type is always None here -- see
    migration 007 for why the cores table exists."""
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


def upsert_product(conn, brand_id: str, parsed: dict) -> dict:
    """Returns {"product_id": ..., "pending_image_jobs": [...]}, same
    shape as product_scraper.upsert_product -- see that module for why
    (image insert is "on conflict do update ... returning id, stored_url"
    specifically so this can tell new/pre-existing rows apart without a
    second query)."""
    weights_range = None
    if parsed["weights_available"]:
        low, high = parsed["weights_available"]
        weights_range = f"[{low},{high}]"

    core_id = get_or_create_core_id(conn, brand_id, parsed.get("core_name"))

    # See product_scraper.upsert_product / migration 003 for the
    # discontinued_detected_at reasoning -- same logic here. Extra
    # relevance for MOTIV specifically: this is the ONLY signal this
    # platform has for a status transition at all (see this module's
    # docstring point 1 -- no on-page indicator), so this timestamp is
    # more load-bearing here than for the other two platforms.
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into products (
                brand_id, name, url, color, coverstock_material, coverstock_type,
                coverstock_name, factory_finish, part_number, weights_available,
                status, source_platform, release_date, description, discontinued_detected_at,
                core_id
            )
            values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::int4range, %s, 'netsuite', %s, %s,
                case when %s = 'retired' then now() else null end,
                %s
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
                parsed["status"], core_id,
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
    that module's docstring for why (each Lambda here is its own
    independent deployment package)."""
    sent = 0
    for i in range(0, len(message_bodies), 10):
        chunk = message_bodies[i:i + 10]
        entries = [{"Id": str(idx), "MessageBody": body} for idx, body in enumerate(chunk)]
        sqs_client.send_message_batch(QueueUrl=queue_url, Entries=entries)
        sent += len(chunk)
    return sent


def _extract_jobs(event: dict) -> list:
    """Same two-shape support (SQS batch or direct invoke) as the other
    two product scrapers. Job dict here also carries "status" (set by
    netsuite_url_discovery, since it's the only reliable status signal
    for this platform -- see module docstring point 1); defaults to
    "current" if absent, e.g. for a manual test invocation."""
    if "Records" in event:
        return [(json.loads(r["body"]), r["messageId"]) for r in event["Records"]]
    return [(event, None)]


def _process_one(job: dict, sqs_client) -> dict:
    url = job["url"]
    brand_id = job["brand_id"]
    status = job.get("status", "current")

    logger.info("Scraping %s", url)
    html = fetch_page(url)
    parsed = parse_product_page(html, url, status=status)

    conn = get_db_connection()
    try:
        result = upsert_product(conn, brand_id, parsed)
    finally:
        conn.close()

    product_id = result["product_id"]
    logger.info("Upserted product %s (%d SKUs)", product_id, len(parsed["skus"]))

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
    """Handles both an SQS-triggered batch (NetsuiteProductScrapeQueue,
    populated by NetsuiteUrlDiscoveryFunction) and a direct/manual
    invocation with {"url": "...", "brand_id": "...", "status": "current"|
    "retired"}, same batchItemFailures pattern as product_scraper's
    handler."""
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
