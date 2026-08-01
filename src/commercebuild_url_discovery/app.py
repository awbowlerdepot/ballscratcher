"""
URL discovery for the commercebuild brand family (Storm, Roto Grip, 900
Global -- one site, stormbowling.com, three brands). See
COMMERCEBUILD_SCOPING.md and commercebuild_product_scraper/app.py's module
docstring for the full research trail.

**Two discovery sources, unioned per brand, both confirmed real this
session (a later session than COMMERCEBUILD_SCOPING.md's original
research -- see below for what changed):**

1. The "Bowling Balls" category listing, filtered per brand via a
   `custom1` facet query param, e.g.:
     https://www.stormbowling.com/products/equipment/bowling-balls/?per_page=100&filter[custom1][0]=Roto-Grip
   CURRENT products only. Real counts as of the original research session:
   Storm 41, Roto Grip 15, 900 Global 5 (61 total) -- well under a single
   per_page=100 page, so this deliberately does NOT implement pagination
   crawling (unlike url_discovery.py's Craft-CMS sitemap approach or
   woocommerce_url_discovery.py's page-by-page crawl). If the catalog
   grows past 100 for any one brand, this under-counts silently -- see
   discover_urls_for_brand()'s docstring for the mitigation (logs a
   warning if the returned count looks like it hit the per_page ceiling).

2. `sitemap_products.xml` (see discover_urls_from_sitemap() below) --
   covers BOTH current and archived products, unioned into each brand's
   URL set alongside the listing above (harmless overlap on current
   products, since diff_against_known() dedupes against discovered_urls
   by URL either way).

**Real finding this session that changes the picture from
COMMERCEBUILD_SCOPING.md's original research:** that doc's one remaining
open risk was that the "Bowling Balls Archive" collection listing's own
product links 404 on a bare request. Root cause is now confirmed: that
collection URL (`/products/featured/bowling-balls-archive/`) 302-redirects
to `/user/login` -- it's gated behind authentication now (confirmed via
curl -sD, real `location: https://www.stormbowling.com/user/login`
header), which is why following its own links with a plain cookie-less
request always landed on a login page rather than the product. No
Referer/cookie workaround was needed or tried further once this was
understood -- the collection listing simply isn't usable as a discovery
source at all anymore, gated or not.

The real fix: `sitemap_index.xml` (confirmed real, plain XML, not
gzipped despite earlier suspicion) points to `sitemap_products.xml`,
which is public, returns 958 real `<loc>` entries in flat canonical URL
form, and was confirmed via curl to include archived products directly
(`900-global-altered-reality-bowling-ball`, `storm-absolute-bowling-ball`)
alongside current ones (`storm-alpha-crux-bowling-ball`) -- no auth, no
collection-listing crawl needed. This sitemap also contains non-ball
merchandise (bags, apparel, accessories -- all share the same brand-name
URL prefix, e.g. `roto-grip-classic-hoodie`), so discover_urls_from_sitemap()
below can't reliably pre-filter to balls-only by URL shape alone;
commercebuild_product_scraper.py's classify_product_status() does that
filtering per-page instead (via each page's own breadcrumb trail), and
skips non-ball products gracefully rather than erroring -- see that
function's docstring.

No sitemap <lastmod> per-product-URL entry was found in the real fetched
sitemap_products.xml this session (only the top-level sitemap_index.xml's
per-sitemap-FILE lastmod, not per-URL) -- diff_against_known() below still
only ever marks a URL "new" or "unchanged", never "changed", same real
gap as before.

robots.txt sets `Crawl-delay: 10` for stormbowling.com (confirmed real,
fetched directly this session) -- Brunswick's site had no such directive.
handler() sleeps 10s between each of the three brands' listing fetches to
respect it. This does NOT rate-limit CommercebuildProductScraperFunction's
own fetches (each SQS-triggered invocation only makes 1-2 requests, so
per-invocation sleeping doesn't help) -- that needs to be handled via SQS
concurrency limits in template.yaml instead, see the comment there. The
sitemap fetch itself happens once per handler() invocation (not per
brand), so it doesn't need its own crawl-delay sleep.
"""
import json
import logging
import os
import re
import time
from urllib.parse import urljoin

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_CATEGORY_URL = "https://www.stormbowling.com/products/equipment/bowling-balls/"

# custom1 facet values confirmed real this session via the listing page's
# own facet counts (Storm (41), Roto Grip (15), 900 Global (5)).
BRAND_FILTERS = {
    "storm": "storm",
    "roto_grip": "Roto-Grip",
    "global_900": "900-Global",
}

PER_PAGE = 100  # comfortably above the largest real per-brand count seen (41) -- see module docstring's caveat if the catalog grows

# Flat canonical product URL shape, confirmed real via curl against a
# filtered Roto Grip listing this session -- every one of that brand's 15
# real products linked via this exact pattern, each appearing twice
# (image link + title link), naturally deduplicated by the set() below.
PRODUCT_LINK_RE = re.compile(r'href="(/[a-z0-9-]+-bowling-ball)"')

CRAWL_DELAY_SECONDS = 10  # robots.txt Crawl-delay: 10, confirmed real this session


def fetch_page(url: str, timeout: int = 30) -> str:
    """Fetch raw HTML. Kept separate from parsing so tests can feed real
    fixture HTML without a network call."""
    import requests

    resp = requests.get(url, headers={"User-Agent": "bowling-scraper/1.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_listing_page(html: str, base_url: str) -> set:
    """Returns the set of absolute product URLs linked from a (possibly
    brand-filtered) category listing page."""
    return {urljoin(base_url, m) for m in PRODUCT_LINK_RE.findall(html)}


def build_listing_url(brand_filter: str, category_url: str = DEFAULT_CATEGORY_URL, per_page: int = PER_PAGE) -> str:
    return f"{category_url}?per_page={per_page}&sort_by=1&filter%5Bcustom1%5D%5B0%5D={brand_filter}"


def discover_urls_for_brand(fetch_fn, brand_filter: str, category_url: str = DEFAULT_CATEGORY_URL, per_page: int = PER_PAGE) -> set:
    """Fetches one brand's filtered listing (single page, no pagination --
    see module docstring) and returns its product URL set. Warns (doesn't
    fail) if the result count looks like it may have hit the per_page
    ceiling, since that would mean silent under-counting rather than an
    error."""
    url = build_listing_url(brand_filter, category_url, per_page)
    html = fetch_fn(url)
    urls = parse_listing_page(html, url)

    if len(urls) >= per_page:
        logger.warning(
            "discover_urls_for_brand(%s) found %d URLs, >= per_page=%d -- "
            "the catalog may have grown past what a single page returns; "
            "this function doesn't paginate, results may be incomplete",
            brand_filter, len(urls), per_page,
        )

    return urls


DEFAULT_SITEMAP_URL = "https://www.stormbowling.com/sitemap_products.xml"

# Plain regex, not an XML parser -- consistent with this module's other
# link-extraction functions, and sitemap_products.xml's <loc> entries are
# simple generated URLs with no entity-escaping seen in this session's real
# sample.
_LOC_RE = re.compile(r"<loc>([^<]+)</loc>")

# Confirmed real via curl this session: every commercebuild product URL
# (ball or not, current or archived) in the sitemap is a single flat path
# segment under the site root, prefixed by its brand's slug -- e.g.
# storm-alpha-crux-bowling-ball, storm-absolute-bowling-ball (archived),
# roto-grip-classic-hoodie (non-ball). The one confirmed real exception is
# a nested-path entry also seen in the sitemap
# (/products/featured/bowling-balls-archive/bbproi-roto-grip-clear-poly) --
# excluded by requiring a single path segment, same as this project's
# other "flat canonical URL" assumptions.
_FLAT_SLUG_RE = re.compile(r"^https://www\.stormbowling\.com/([a-z0-9-]+)$")

_SITEMAP_BRAND_PREFIXES = (
    ("storm", "storm-"),
    ("roto_grip", "roto-grip-"),
    ("global_900", "900-global-"),
)


def classify_sitemap_url(url: str):
    """Returns the BRAND_FILTERS-matching brand key ("storm"/"roto_grip"/
    "global_900") for a flat, single-path-segment sitemap URL whose slug
    starts with that brand's prefix, or None if it isn't a flat
    single-segment URL at all, or doesn't match any brand prefix.

    Deliberately NOT trying to also filter out non-ball merchandise here
    (bags/apparel/accessories share the same brand-prefixed URL shape,
    confirmed real this session, e.g. roto-grip-classic-hoodie) -- that
    filtering happens per-page in commercebuild_product_scraper.py's
    classify_product_status(), which reads each page's own breadcrumb
    trail rather than guessing from the URL. If a non-product nav page
    (e.g. /storm-brand) or some other stray slug ever matches a brand
    prefix here, that same breadcrumb check catches and skips it
    gracefully at scrape time -- so this function doesn't need to be
    airtight, just a reasonable pre-filter to avoid discovering entirely
    unrelated sitemap entries."""
    m = _FLAT_SLUG_RE.match(url)
    if not m:
        return None
    slug = m.group(1)
    for brand_key, prefix in _SITEMAP_BRAND_PREFIXES:
        if slug.startswith(prefix):
            return brand_key
    return None


def discover_urls_from_sitemap(fetch_fn, sitemap_url: str = DEFAULT_SITEMAP_URL) -> dict:
    """Fetches sitemap_products.xml ONCE (958 real entries confirmed this
    session, public, no auth needed -- see module docstring for why this
    replaces the now-login-gated "Bowling Balls Archive" collection
    listing) and buckets its URLs by brand key. Doesn't distinguish
    current vs. archived vs. non-ball here -- that's decided per-page at
    scrape time (see classify_sitemap_url's docstring). Returns
    {"storm": set(), "roto_grip": set(), "global_900": set()}, always all
    three keys present (possibly empty) so callers don't need a
    .get(..., set()) at every use site."""
    xml_text = fetch_fn(sitemap_url)
    buckets = {"storm": set(), "roto_grip": set(), "global_900": set()}
    for loc in _LOC_RE.findall(xml_text):
        brand_key = classify_sitemap_url(loc)
        if brand_key:
            buckets[brand_key].add(loc)
    return buckets


def build_entries(urls: set) -> list:
    """No sitemap lastmod source yet (see module docstring) -- every
    entry gets lastmod=None."""
    return [{"url": url, "lastmod": None} for url in sorted(urls)]


def diff_against_known(conn, brand_id: str, entries: list) -> dict:
    """Same diff shape as the other url_discovery modules, against the
    same discovered_urls table. With lastmod always None here (see module
    docstring), a URL can only ever be "new" (first time) or "unchanged"
    (every subsequent run) -- "changed" never fires. Duplicated rather
    than imported cross-module, same reasoning as every other scraper
    pair in this project (see product_scraper/app.py's publish_messages
    docstring)."""
    new_urls, changed_urls, unchanged_urls = [], [], []

    with conn.cursor() as cur:
        for entry in entries:
            cur.execute(
                "select sitemap_lastmod from discovered_urls where url = %s",
                (entry["url"],),
            )
            row = cur.fetchone()

            if row is None:
                cur.execute(
                    """
                    insert into discovered_urls (brand_id, url, sitemap_lastmod)
                    values (%s, %s, %s)
                    """,
                    (brand_id, entry["url"], entry["lastmod"]),
                )
                new_urls.append(entry["url"])
            else:
                cur.execute(
                    "update discovered_urls set last_seen_at = now() where url = %s",
                    (entry["url"],),
                )
                unchanged_urls.append(entry["url"])
    conn.commit()

    return {"new": new_urls, "changed": changed_urls, "unchanged": unchanged_urls}


def build_scrape_messages(brand_id: str, urls: list) -> list:
    return [json.dumps({"url": url, "brand_id": brand_id}) for url in urls]


def publish_messages(sqs_client, queue_url: str, message_bodies: list) -> int:
    sent = 0
    for i in range(0, len(message_bodies), 10):
        chunk = message_bodies[i:i + 10]
        entries = [{"Id": str(idx), "MessageBody": body} for idx, body in enumerate(chunk)]
        sqs_client.send_message_batch(QueueUrl=queue_url, Entries=entries)
        sent += len(chunk)
    return sent


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


def handler(event, context):
    """Crawls all three brands' current-product listings (one page each,
    10s apart per robots.txt Crawl-delay) AND the shared
    sitemap_products.xml (fetched once, covers current+archived+non-ball
    -- see module docstring), unions each brand's two URL sets, diffs
    against discovered_urls, and publishes new URLs to
    CommercebuildProductScrapeQueue. Non-ball URLs that slip through the
    sitemap's brand-prefix pre-filter are skipped gracefully at scrape
    time, not here -- see commercebuild_product_scraper.py's
    classify_product_status().

    Brand IDs come from BRAND_IDS_JSON, a JSON object mapping the same
    keys as BRAND_FILTERS ("storm", "roto_grip", "global_900") to real
    brands.id UUIDs -- set once those three brand rows are seeded (see
    DEPLOY_RUNBOOK.md). A brand with no id set in that mapping is skipped
    entirely (logged, not a hard failure) rather than crashing the whole
    run over one brand not being onboarded yet."""
    brand_ids = json.loads(os.environ.get("BRAND_IDS_JSON", "{}"))
    category_url = os.environ.get("CATEGORY_URL", DEFAULT_CATEGORY_URL)
    sitemap_url = os.environ.get("SITEMAP_URL", DEFAULT_SITEMAP_URL)
    queue_url = os.environ.get("PRODUCT_SCRAPE_QUEUE_URL")

    conn = get_db_connection()
    per_brand_results = {}
    try:
        import boto3
        sqs = boto3.client("sqs") if queue_url else None

        # Fetched once for all three brands (not per-brand -- see module
        # docstring), before the crawl-delay-respecting per-brand loop
        # below. A sitemap failure shouldn't take down current-product
        # discovery, which has worked in production since this platform's
        # first deploy -- log and continue with an empty sitemap result
        # rather than failing the whole run.
        try:
            sitemap_buckets = discover_urls_from_sitemap(fetch_page, sitemap_url)
        except Exception:
            logger.exception(
                "Failed to fetch/parse commercebuild sitemap at %s -- continuing with "
                "current-product listing crawl only; archived products won't be "
                "discovered this run", sitemap_url,
            )
            sitemap_buckets = {}

        first = True
        for brand_key, filter_value in BRAND_FILTERS.items():
            brand_id = brand_ids.get(brand_key)
            if not brand_id:
                logger.info("Skipping brand %r -- no brand_id configured in BRAND_IDS_JSON", brand_key)
                continue

            if not first:
                time.sleep(CRAWL_DELAY_SECONDS)
            first = False

            logger.info("Discovering %s (filter=%r) current-product URLs", brand_key, filter_value)
            current_urls = discover_urls_for_brand(fetch_page, filter_value, category_url)
            sitemap_urls = sitemap_buckets.get(brand_key, set())
            urls = current_urls | sitemap_urls
            logger.info(
                "%s: %d from category listing (current only), %d from sitemap "
                "(current+archived+non-ball, filtered per-page at scrape time), "
                "%d combined",
                brand_key, len(current_urls), len(sitemap_urls), len(urls),
            )
            entries = build_entries(urls)
            diff = diff_against_known(conn, brand_id, entries)

            published = 0
            urls_to_scrape = diff["new"] + diff["changed"]
            if urls_to_scrape and sqs:
                messages = build_scrape_messages(brand_id, urls_to_scrape)
                published = publish_messages(sqs, queue_url, messages)

            per_brand_results[brand_key] = {
                "total_seen": len(entries),
                "new_count": len(diff["new"]),
                "unchanged_count": len(diff["unchanged"]),
                "published_count": published,
            }
            logger.info("%s: %s", brand_key, per_brand_results[brand_key])
    finally:
        conn.close()

    return {"statusCode": 200, "body": json.dumps({"brands": per_brand_results})}
