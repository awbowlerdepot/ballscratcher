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
   front, side/back, and a third angle).

   HISTORICAL, no longer active (see the "THIRD real finding" section
   further down this docstring for the removal): this platform ALSO had a
   "core cutaway" shot on a <div class="image"> inside the per-weight
   carousel, served from a different path,
   "userfiles/filemanager-format/core-image/<id>", a transform of one of
   the main gallery's own image ids -- i.e. always a redundant, lower-res
   duplicate of a photo already captured from the main gallery, never a
   genuinely new one. Confirmed and removed once Al pointed it out
   directly. The rest of this point's history is kept for context (the
   empty-path bug below shaped this module's general "skip a known non-
   image placeholder shape" pattern, still in use elsewhere), even though
   parse_images() no longer scans that container at all.

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
   ajax-loader.gif/coming_soon.jpg skip -- see _extract_background_
   images()'s docstring for the general fix (skip any captured URL with
   nothing after its final "/"), kept in place even though the specific
   container that motivated it is no longer scanned.

   SECOND real bug, found via a live data-quality pass (Al noticed
   "aggressive" image pulling -- 3 real per-product photos plus "a bunch
   that are just on all product pages"): the previous version ran
   IMAGE_RE across the ENTIRE raw HTML with no DOM scoping at all, only
   requiring "userfiles/filemanager" to appear in the url(...) -- but
   that's MOTIV's general asset CDN path, used for every product's
   photos site-wide, not something scoped to just the current page's own
   ball. Whatever else lives on the page and links to another product's
   photo under that same path (a cross-sell/"related products" strip,
   most likely, though the exact section wasn't captured this session --
   see the new fixture comments for what's confirmed vs. a plausible
   reconstruction) got vacuumed up as if it belonged to this product.
   Fixed by scoping to two specific, real containers instead of the whole
   page -- confirmed live via Al inspecting the real DOM and providing the
   exact selector: `body > main > section.product > div > div > div >
   div.image-scroll-wrapper > ul` for the main gallery. That's a deep,
   fragile-looking path, but `div.image-scroll-wrapper` is the only
   class-named segment in it and unlikely to collide with anything else
   on the page, so parse_images() anchors on that one class rather than
   the full child chain (robust to the anonymous wrapper divs around it
   changing depth). The core-cutaway shot lives in a separate, already-
   documented container -- div.product-specifications-by-weight (the
   per-weight carousel) -- scoped there for the same reason. Anything
   with a background-image style OUTSIDE both containers is no longer
   even looked at, regardless of what path it happens to use.

   Al directly asked whether scripts/rescrape_netsuite_products.py would
   actually REMOVE the already-wrong image rows an already-scraped MOTIV
   product might still have, not just stop adding new ones -- a fair
   question, and the honest answer at that point was no: upsert_product's
   image step was a plain insert-on-conflict-update, which only ever adds
   a row for a source_url still present or updates one that already
   matches; it never deletes a row for a source_url that's no longer part
   of what got parsed. So a rescrape under the fixed parser would have
   just added the correct 3-4 photos ALONGSIDE whatever wrong ones were
   already sitting there, not replaced them. Fixed in upsert_product
   (see its own docstring) by deleting any product_images row for this
   product_id whose source_url isn't in the current parse's set, after
   the insert/update loop -- a rescrape now genuinely replaces the image
   set instead of just extending it.

   Al's immediate next question: does that DB delete also clean up the S3
   objects image_processor may have already mirrored for a since-deleted
   row? At first, no -- deleting the row didn't touch S3 at all, leaving
   those objects orphaned. Fixed by having upsert_product's DELETE return
   the removed rows (id + stored_url) instead of just deleting blind, and
   adding delete_orphaned_image_objects() (called from _process_one, see
   its own docstring) to remove the matching S3 objects for any deleted
   row that actually had one. Gated on a new IMAGE_BUCKET env var --
   optional/soft-fail, same convention as IMAGE_PROCESS_QUEUE_URL: a
   deployment that hasn't wired IMAGE_BUCKET onto this function yet just
   logs a warning and skips the S3 cleanup for that run rather than
   erroring; the DB-side fix works regardless.

   THIRD real finding, same thread: Al then asked directly why the
   core-cutaway shot was even being captured at all, pointing out it's
   redundant -- and he's right, confirmed by this module's own earlier
   documentation (point 7 above): the core-image path is "a transform of
   one of the main gallery's own image ids", i.e. the exact same source
   photo MOTIV's template already serves (at a different, lower-
   resolution CDN format) as one of the 3 real gallery photos in
   div.image-scroll-wrapper. Capturing it separately from div.product-
   specifications-by-weight never added a genuinely different photo --
   just a redundant, lower-res duplicate of something already stored,
   sitting in a per-weight carousel that's below the fold and not
   something a site visitor scrolls back up to see differently rendered.
   Removed entirely: parse_images() no longer scans div.product-
   specifications-by-weight at all, and "core_callout" is no longer a
   possible image_type this scraper produces. The empty-path placeholder
   bug this module documented above (point 7's first "real bug") is now
   moot for the same reason -- there's nothing left that would ever look
   at that container's markup, empty or not. A rescrape (scripts/
   rescrape_netsuite_products.py) cleans up any already-stored
   core_callout rows for free: the existing stale-image DELETE in
   upsert_product (see its own docstring) already treats any source_url
   no longer present in the current parse as stale, and core_callout rows
   will simply never appear in that set again -- including the S3 orphan
   cleanup this same delete step now triggers (see delete_orphaned_
   image_objects), no separate migration/backfill needed.

REAL INCIDENT, found via a live data-quality pass (Al noticed every
product on the admin site's MOTIV catalog showed as "current", which
looked wrong given how many retired balls MOTIV has): confirmed via
`select status_path, count(*) from discovered_urls ... group by
status_path` that discovery had correctly found and classified 434 MOTIV
URLs (60 current, 374 retired) -- the bug wasn't in netsuite_url_
discovery. But every single one of the 202 products actually scraped
into `products` showed status='current', including ones that must have
originated from a 'retired'-classified discovered_urls row (202 > 60).
Root cause: `_process_one` used to do `job.get("status", "current")` with
no other fallback. That's correct for jobs netsuite_url_discovery
publishes (always includes status), but admin_api's queue_rescrape --
the generic "Rescrape" button and scripts/backfill_core_ids.py's
catalog-wide core backfill, shared across all five scraper platforms --
only ever publishes {"url", "brand_id"}, no status key at all (Brunswick/
SWAG don't need one; they infer status from the page/URL itself).
NetSuite was the one platform that both has no on-page status signal
AND blindly defaulted to "current" instead of looking one up. Worse,
upsert_product's `status = excluded.status` unconditionally overwrites
(no coalesce-preserve-existing fallback the way release_date/description/
core_id have), so every status-less rescrape permanently clobbered a
retired MOTIV product back to "current" -- most likely explanation for
how all 202 ended up this way, given this catalog went through exactly
that catalog-wide core backfill in an earlier session. Fixed by adding
get_status_for_url() (mirrors shopify_product_scraper's function of the
same name, same underlying reason: no on-page status signal) as a
fallback for any job that omits status, rather than blindly defaulting.
This only fixes it going forward for a fresh scrape -- see scripts/
backfill_netsuite_status.py for the one-off correction needed to fix the
202 already-wrong rows without waiting for their next natural re-scrape.
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


def _extract_background_images(container, base_url: str, seen: set, images: list, image_type_for) -> None:
    """Shared helper for parse_images()'s two scoped containers -- walks
    every descendant with a style attribute (not just <a>/<div>, so this
    doesn't care which tag MOTIV's template happens to use) and pulls out
    any background-image url(...) matching IMAGE_RE (still restricted to
    userfiles/filemanager paths, on top of the container scoping itself --
    belt and suspenders). image_type_for is a callable, not a fixed
    string, since the main gallery's first real photo is "main" and every
    photo after it is "other" (see parse_images' own docstring)."""
    if container is None:
        return
    for el in container.find_all(style=True):
        match = IMAGE_RE.search(el["style"])
        if not match:
            continue
        src = urljoin(base_url, match.group(1))
        if src in seen:
            continue
        seen.add(src)

        # Real bug found via production DLQ investigation: products with
        # no real core-cutaway photo get a background-image style with an
        # EMPTY path (confirmed real:
        # `url(./userfiles/filemanager-format/core-image/)`, nothing
        # after the trailing slash) -- this still matches IMAGE_RE but
        # isn't a real image, and 404s forever in image_processor no
        # matter how many times it's retried. Skipped by requiring at
        # least one non-slash character after the URL's final "/" -- same
        # skip-a-known-non-image-shape spirit as Brunswick's data: URI
        # check.
        if src.endswith("/"):
            continue

        images.append({"image_type": image_type_for(), "source_url": src})


def parse_images(soup: BeautifulSoup, base_url: str) -> list:
    """DOM-scoped (not a raw-HTML regex sweep) -- see module docstring
    point 7's "SECOND real bug" section for why: the old version matched
    IMAGE_RE against the entire page, and MOTIV's userfiles/filemanager
    path is the site's general photo CDN, not something scoped to just
    the current product -- so anything else on the page linking to
    ANOTHER product's photo under that same path (e.g. a cross-sell strip
    that appears on every product page) got wrongly attached to whichever
    product happened to be scraped.

    Scoped to div.image-scroll-wrapper -- the main gallery (3 real photos:
    front, side/back, third angle), confirmed via a live DOM inspection.
    Al's own real selector was `body > main > section.product > div > div
    > div > div.image-scroll-wrapper > ul` -- anchored here on just the
    class-named segment (image-scroll-wrapper) rather than the full
    child-index chain, since the anonymous wrapper divs around it are
    exactly the kind of thing that shifts if MOTIV's template changes
    without the gallery itself moving. Anything with a background-image
    style outside this container is never even looked at, regardless of
    what path it uses.

    Does NOT also scan div.product-specifications-by-weight for a
    "core cutaway" shot anymore (see module docstring point 7's
    "THIRD real finding" -- Al pointed out directly that it was always a
    redundant, lower-resolution duplicate of a photo already captured
    from this same gallery, never a genuinely different one, and sitting
    in a per-weight carousel below the fold that nobody would separately
    see anyway)."""
    images = []
    seen = set()

    gallery = soup.select_one("div.image-scroll-wrapper")
    _extract_background_images(gallery, base_url, seen, images, lambda: "main" if not images else "other")

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
        "images": parse_images(soup, url),
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


def get_status_for_url(conn, url: str) -> str:
    """Real, confirmed bug fix: falls back to the status_path
    netsuite_url_discovery already resolved and stored on discovered_urls
    (see this module's docstring point 1) for any job that doesn't
    explicitly carry its own "status". Same lookup shopify_product_
    scraper.get_status_for_url already does, for the same underlying
    reason -- neither platform has an on-page status signal of its own.

    Why this was needed: _process_one used to do `job.get("status",
    "current")` with no fallback at all, which is correct for jobs
    netsuite_url_discovery publishes (always includes status) but silently
    wrong for jobs published by admin_api's queue_rescrape (the "Rescrape"
    button, and scripts/backfill_core_ids.py's catalog-wide core backfill)
    -- that function is shared across all five scraper platforms and only
    ever publishes {"url", "brand_id"}, no status key, since Brunswick/SWAG
    infer status from the page/URL itself and don't need one. NetSuite was
    the only platform that both (a) has no on-page status signal and (b)
    blindly defaulted to "current" instead of looking it up -- and
    upsert_product's `status = excluded.status` has no coalesce-preserve-
    existing fallback the way release_date/description/core_id do, so
    every status-less rescrape permanently clobbered a retired MOTIV
    product back to "current". Confirmed live on MOTIV's actual catalog:
    every one of 202 scraped products showed status='current' despite
    discovered_urls correctly holding 374 'retired' vs. 60 'current'
    entries from the original discovery crawl -- almost certainly the
    catalog-wide core backfill silently re-flipping all of them.

    Defaults to 'current' when the URL isn't in discovered_urls at all
    (e.g. a manual/direct scrape of a URL nobody's ever discovered through
    the normal category-crawl path) -- same "presumably still live" call
    shopify_product_scraper makes, rather than failing the NOT NULL
    products.status constraint."""
    with conn.cursor() as cur:
        cur.execute("select status_path from discovered_urls where url = %s", (url,))
        row = cur.fetchone()
    if row is None or row[0] is None:
        return "current"
    return row[0]


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
    duplicated rather than shared, same reasoning as
    get_or_create_core_id above. See migration 008 for why this table
    exists, and _normalize_coverstock_name above for why the lookup key
    isn't just coverstock_name verbatim."""
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
    coverstock_id = get_or_create_coverstock_id(
        conn, brand_id, parsed.get("coverstock_name"),
        parsed.get("coverstock_material"), parsed.get("coverstock_type"),
    )

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
                core_id, coverstock_id
            )
            values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::int4range, %s, 'netsuite', %s, %s,
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
            current_source_urls.add(image["source_url"])
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

        # Real cleanup step, added alongside parse_images' DOM-scoping fix
        # (see that function's docstring, "SECOND real bug", and DEPLOY_
        # RUNBOOK.md 6e.6): a plain upsert on its own never REMOVES a row
        # -- it only inserts new source_urls or updates ones still present.
        # Al asked directly whether scripts/rescrape_netsuite_products.py
        # would actually clear the wrong image rows a MOTIV product
        # already scraped under the old unscoped regex might still have --
        # without this delete, the answer was no: a rescrape would just
        # add the correct 3-4 photos alongside whatever wrong ones were
        # already sitting there, never removing them. This makes a
        # rescrape genuinely REPLACE the image set with whatever the
        # current parse actually found, not just extend it.
        #
        # `returning id, stored_url` -- Al's own next question: does this
        # also clean up the S3 objects those rows may have already been
        # mirrored to (see image_processor.upload_variants)? A bare DELETE
        # would leave those orphaned forever, since nothing else in this
        # codebase ever revisits an already-processed image. Returning the
        # deleted rows here (rather than a bare DELETE) is what lets
        # _process_one below actually clean those up -- see
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
            # parsed["images"] came back empty this time -- every
            # existing row for this product is stale by definition.
            cur.execute(
                "delete from product_images where product_id = %s returning id, stored_url",
                (product_id,),
            )
        stale_image_rows = [{"id": row[0], "stored_url": row[1]} for row in cur.fetchall()]

    conn.commit()
    return {"product_id": product_id, "pending_image_jobs": pending_image_jobs, "stale_image_rows": stale_image_rows}


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


def delete_orphaned_image_objects(s3_client, bucket: str, stale_image_rows: list) -> int:
    """Real follow-up Al asked for directly: upsert_product's stale-row
    DELETE (see its own docstring) clears the product_images DB row, but
    does nothing about the S3 objects image_processor may have already
    uploaded for that row -- left orphaned otherwise, forever, since
    nothing else in this codebase ever revisits an already-processed
    image_id.

    Only bothers with rows that actually have a stored_url (`stored_url
    is not None` -- see the `stale_image_rows` filtering in
    _process_one): a row still awaiting image_processor never had
    anything uploaded for it, so there's nothing in S3 to clean up.

    Mirrors image_processor.upload_variants' real key convention
    (`product-images/<product_image_id>/<size_name>.png`) rather than
    importing it -- separate Lambda deployment packages, same "own the
    whole package" duplication convention as publish_messages above and
    the other *_product_scraper modules generally. Lists each id's
    objects (via `list_objects_v2`, paginated) and deletes whatever comes
    back, rather than hard-coding image_processor's current SIZE_PRESETS
    name set (thumbnail/catalog/detail) here too -- correct even if that
    preset list changes later without this module needing to track it.

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
    """Same two-shape support (SQS batch or direct invoke) as the other
    two product scrapers. Job dict here also carries "status" (set by
    netsuite_url_discovery, since it's the only reliable status signal
    for this platform -- see module docstring point 1) -- REAL BUG,
    CONFIRMED AND FIXED this session: this used to default to "current"
    right here when status was absent, correct for a manual test
    invocation but silently wrong for admin_api's queue_rescrape (the
    "Rescrape" button / scripts/backfill_core_ids.py), which never
    includes a status key at all. See get_status_for_url's docstring and
    _process_one below for the actual fix -- status is no longer defaulted
    at extraction time; a missing key now falls back to a fresh
    discovered_urls lookup instead of a blind guess."""
    if "Records" in event:
        return [(json.loads(r["body"]), r["messageId"]) for r in event["Records"]]
    return [(event, None)]


def _process_one(job: dict, sqs_client, s3_client=None) -> dict:
    url = job["url"]
    brand_id = job["brand_id"]

    conn = get_db_connection()
    try:
        # job["status"] wins when the job explicitly provides one (the
        # normal netsuite_url_discovery-triggered path). Falls back to
        # get_status_for_url's discovered_urls lookup otherwise -- see
        # that function's docstring for the real, confirmed bug this
        # closes (a status-less rescrape used to silently reset a
        # product's status to "current", permanently, since upsert_
        # product's `status = excluded.status` has no coalesce-preserve-
        # existing fallback).
        status = job.get("status") or get_status_for_url(conn, url)

        logger.info("Scraping %s (status=%s)", url, status)
        html = fetch_page(url)
        parsed = parse_product_page(html, url, status=status)

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

    # Real follow-up Al asked for directly: does a rescrape also clean up
    # the S3 objects a since-deleted product_images row may have already
    # had mirrored to S3? See upsert_product's stale_image_rows and
    # delete_orphaned_image_objects's own docstring for the full story.
    # image_bucket is optional (soft-fails, same convention as
    # image_queue_url above) -- a deployment that hasn't set IMAGE_BUCKET
    # on this function yet just skips cleanup and logs why, rather than
    # erroring; the DB-side fix (the delete itself) already works either
    # way.
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
        "image_jobs_published": image_jobs_published,
        "orphaned_objects_deleted": orphaned_objects_deleted,
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

    # s3_client for delete_orphaned_image_objects (see _process_one) --
    # built once here and reused across every job in this batch, same
    # "build the client once per invocation" pattern as sqs_client above.
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
                raise

    response = {"statusCode": 200, "body": json.dumps({"results": results})}
    if "Records" in event:
        response["batchItemFailures"] = batch_item_failures
    return response
