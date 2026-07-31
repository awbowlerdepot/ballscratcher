"""
URL discovery for the commercebuild brand family (Storm, Roto Grip, 900
Global -- one site, stormbowling.com, three brands). See
COMMERCEBUILD_SCOPING.md and commercebuild_product_scraper/app.py's module
docstring for the full research trail.

**Current products only** -- see commercebuild_product_scraper/app.py's
docstring for why archived/retired products need a different scraper AND
a different URL-discovery approach (their collection listing's own links
404 on a bare request, confirmed real this session).

Discovery source: the "Bowling Balls" category listing, filtered per
brand via a `custom1` facet query param, e.g.:
  https://www.stormbowling.com/products/equipment/bowling-balls/?per_page=100&filter[custom1][0]=Roto-Grip
Confirmed real this session via curl: real counts as of this session were
Storm 41, Roto Grip 15, 900 Global 5 (61 total) -- well under a single
per_page=100 page, so this deliberately does NOT implement pagination
crawling (unlike url_discovery.py's Craft-CMS sitemap approach or
woocommerce_url_discovery.py's page-by-page crawl). If the catalog grows
past 100 for any one brand, this under-counts silently -- see
discover_urls_for_brand()'s docstring for the mitigation (logs a warning
if the returned count looks like it hit the per_page ceiling).

No sitemap dependency, unlike the Craft-CMS/WooCommerce discovery
functions -- sitemap_index.xml exists (confirmed real, returned as a real
XML/gzip response) but was never actually parsed this session (see
COMMERCEBUILD_SCOPING.md's still-open items), so there's no confirmed
<lastmod> source to build entries from yet. This means
diff_against_known() below can only ever mark a URL "new" (first time
seen) or "unchanged" (every later run) -- it can never detect that an
already-known product's page content actually changed, since there's no
lastmod signal to compare. Acceptable for now (same spirit as this
project's other real, disclosed limitations, e.g. SWAG/MOTIV having no
automated schedule yet) but a real gap: a re-scrape of an existing
product currently only happens if you invoke this scraper directly for
that one URL.

robots.txt sets `Crawl-delay: 10` for stormbowling.com (confirmed real,
fetched directly this session) -- Brunswick's site had no such directive.
handler() sleeps 10s between each of the three brands' listing fetches to
respect it. This does NOT rate-limit CommercebuildProductScraperFunction's
own fetches (each SQS-triggered invocation only makes 1-2 requests, so
per-invocation sleeping doesn't help) -- that needs to be handled via SQS
concurrency limits in template.yaml instead, see the comment there.
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
    10s apart per robots.txt Crawl-delay), diffs each brand's URLs against
    discovered_urls, and publishes new URLs to CommercebuildProductScrapeQueue.

    Brand IDs come from BRAND_IDS_JSON, a JSON object mapping the same
    keys as BRAND_FILTERS ("storm", "roto_grip", "global_900") to real
    brands.id UUIDs -- set once those three brand rows are seeded (see
    DEPLOY_RUNBOOK.md). A brand with no id set in that mapping is skipped
    entirely (logged, not a hard failure) rather than crashing the whole
    run over one brand not being onboarded yet."""
    brand_ids = json.loads(os.environ.get("BRAND_IDS_JSON", "{}"))
    category_url = os.environ.get("CATEGORY_URL", DEFAULT_CATEGORY_URL)
    queue_url = os.environ.get("PRODUCT_SCRAPE_QUEUE_URL")

    conn = get_db_connection()
    per_brand_results = {}
    try:
        import boto3
        sqs = boto3.client("sqs") if queue_url else None

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
            urls = discover_urls_for_brand(fetch_page, filter_value, category_url)
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
