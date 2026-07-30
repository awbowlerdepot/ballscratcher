"""
URL discovery for the WooCommerce brand family (SWAG Bowling to start --
swagbowling.com, confirmed live via direct fetch this session: WordPress +
WooCommerce, fully server-rendered, no JS required).

Deliberately NOT a sitemap-diff, unlike url_discovery/app.py (the Craft-CMS
family's approach). Two real, confirmed reasons:

1. Yoast SEO's product-sitemap.xml (the WooCommerce/Yoast equivalent of
   Brunswick's dedicated bowlerProducts sitemap section) is flat and mixes
   EVERY product type -- bowling balls, jerseys, hats, towels -- under the
   same /product/{slug}/ path with no distinguishing path segment. There's
   no Brunswick-style "/products/balls/(current|retired)/" pattern to
   filter on.
2. The category archive pages (confirmed real via direct fetch:
   https://www.swagbowling.com/shop/bowling-balls/, paginated, 96 results
   across 2 pages as of this session) DO cleanly separate bowling balls
   from everything else, and are themselves server-rendered HTML with
   plain <a href="/product/{slug}/"> links -- no JS needed to read them
   either.

So this fetches the category archive pages (following pagination) to get
the *set* of ball product URLs, then cross-references each against the
flat product-sitemap.xml (which still has real per-URL <lastmod> values)
to get the same new/changed/unchanged diff behavior url_discovery/app.py
provides for Brunswick. diff_against_known() is copied rather than shared
across the two functions -- see product_scraper/app.py's publish_messages
docstring for the same reasoning (each Lambda here is its own independent
deployment package, and a shared layer isn't worth it yet for this much
code).

Current/retired status is deliberately NOT determined here, unlike
Brunswick (where it's inferred from the URL path at discovery time).
SWAG's own product pages carry it directly as a WooCommerce attribute
("Production-status": In Production / Discontinued -- confirmed real via
a direct fetch of a real product page this session), which is more
reliable than trying to infer it from category-listing groupings on this
page. See woocommerce_product_scraper/app.py.
"""
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

DEFAULT_CATEGORY_URL = "https://www.swagbowling.com/shop/bowling-balls/"
DEFAULT_SITEMAP_URL = "https://www.swagbowling.com/product-sitemap.xml"

# WooCommerce's default permalink structure for products. Confirmed real
# against every product link seen on swagbowling.com this session (balls,
# apparel, and accessories alike all use this shape) -- matching by this
# pattern rather than any CSS class since WooCommerce themes vary wildly
# in markup/classes but the underlying permalink structure is a WooCommerce
# core convention, not a theme choice.
PRODUCT_LINK_RE = re.compile(r'href="([^"]*/product/[^"/]+/)"')

# WooCommerce/Yoast's default paginated archive URL shape
# (".../page/2/", etc.) -- also a platform convention, not theme-specific.
NEXT_PAGE_RE = re.compile(r'href="([^"]*/page/(\d+)/)"')

MAX_PAGES = 20  # safety cap; SWAG's ball catalog is ~96 products / 2 pages as of this session


def fetch_page(url: str, timeout: int = 30) -> str:
    """Fetch raw HTML. Kept separate from parsing so tests can feed real
    fixture HTML without a network call."""
    import requests

    resp = requests.get(url, headers={"User-Agent": "bowling-scraper/1.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_category_page(html: str, base_url: str) -> set:
    """Returns the set of absolute /product/{slug}/ URLs linked from a
    category archive page. Matches by href pattern rather than any
    specific surrounding markup -- deliberately resilient to WooCommerce
    theme/layout changes, same content-matching philosophy as the
    Craft-CMS scraper."""
    return {urljoin(base_url, m) for m in PRODUCT_LINK_RE.findall(html)}


def find_next_page_url(html: str, base_url: str, current_page: int) -> str:
    """Returns the URL of the next paginated archive page, or None if this
    is the last page. Looks for a "page/{current_page + 1}/" link rather
    than just any "page/N/" link, since archive pages often link to
    several page numbers (1, 2, 3...) at once for navigation."""
    target = current_page + 1
    for path, page_num in NEXT_PAGE_RE.findall(html):
        if int(page_num) == target:
            return urljoin(base_url, path)
    return None


def discover_ball_urls(fetch_fn, start_url: str = DEFAULT_CATEGORY_URL, max_pages: int = MAX_PAGES) -> set:
    """Paginates through the category archive starting at start_url,
    collecting every /product/{slug}/ URL found. fetch_fn is injected
    (rather than calling fetch_page directly) so this is testable against
    fixture HTML without a network call."""
    all_urls = set()
    url = start_url
    page = 1

    while url and page <= max_pages:
        html = fetch_fn(url)
        all_urls |= parse_category_page(html, url)
        next_url = find_next_page_url(html, url, page)
        if next_url == url:
            break  # guard against an accidental self-link loop
        url = next_url
        page += 1

    return all_urls


def fetch_sitemap(url: str, timeout: int = 30) -> bytes:
    import requests

    resp = requests.get(url, headers={"User-Agent": "bowling-scraper/1.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def parse_sitemap_lastmods(xml_bytes: bytes) -> dict:
    """Returns {url: lastmod} for every entry in a Yoast-style flat product
    sitemap. No path filtering here -- that's done separately via the
    category-page discovery, this just supplies lastmod values for
    whatever URLs discover_ball_urls() already decided are balls."""
    root = ET.fromstring(xml_bytes)
    lastmods = {}
    for url_el in root.findall("sm:url", SITEMAP_NS):
        loc_el = url_el.find("sm:loc", SITEMAP_NS)
        if loc_el is None or not loc_el.text:
            continue
        lastmod_el = url_el.find("sm:lastmod", SITEMAP_NS)
        lastmods[loc_el.text.strip()] = lastmod_el.text.strip() if lastmod_el is not None and lastmod_el.text else None
    return lastmods


def build_entries(ball_urls: set, lastmods_by_url: dict) -> list:
    """Combines the category-page-discovered URL set with sitemap lastmod
    values into the {url, lastmod} shape diff_against_known() expects.
    A ball URL missing from the sitemap (edge case -- e.g. a brand-new
    page not yet indexed) still gets included, just with lastmod=None,
    rather than being silently dropped."""
    return [{"url": url, "lastmod": lastmods_by_url.get(url)} for url in sorted(ball_urls)]


def diff_against_known(conn, brand_id: str, entries: list) -> dict:
    """Same new/changed/unchanged diff behavior as
    url_discovery.diff_against_known, against the same discovered_urls
    table -- duplicated rather than imported cross-module, see this
    module's docstring for why. status_path is left null here (unlike the
    Craft-CMS version): SWAG status comes from the product page itself,
    not the URL, so there's nothing to record at discovery time."""
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
                existing_lastmod = row[0]
                incoming_lastmod = entry["lastmod"]
                if incoming_lastmod and str(existing_lastmod) != incoming_lastmod:
                    cur.execute(
                        """
                        update discovered_urls
                        set sitemap_lastmod = %s, last_seen_at = now(), scrape_status = 'pending'
                        where url = %s
                        """,
                        (incoming_lastmod, entry["url"]),
                    )
                    changed_urls.append(entry["url"])
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
    category_url = os.environ.get("CATEGORY_URL", DEFAULT_CATEGORY_URL)
    sitemap_url = os.environ.get("SITEMAP_URL", DEFAULT_SITEMAP_URL)
    brand_id = os.environ["BRAND_ID"]

    logger.info("Discovering ball URLs from %s", category_url)
    ball_urls = discover_ball_urls(fetch_page, category_url)
    logger.info("Found %d ball URLs across category pages", len(ball_urls))

    logger.info("Fetching sitemap %s for lastmod values", sitemap_url)
    xml_bytes = fetch_sitemap(sitemap_url)
    lastmods = parse_sitemap_lastmods(xml_bytes)

    entries = build_entries(ball_urls, lastmods)

    conn = get_db_connection()
    try:
        diff = diff_against_known(conn, brand_id, entries)
    finally:
        conn.close()

    logger.info(
        "Discovery complete: %d new, %d changed, %d unchanged",
        len(diff["new"]), len(diff["changed"]), len(diff["unchanged"]),
    )

    urls_to_scrape = diff["new"] + diff["changed"]
    published_count = 0
    queue_url = os.environ.get("PRODUCT_SCRAPE_QUEUE_URL")
    if urls_to_scrape and queue_url:
        import boto3

        sqs = boto3.client("sqs")
        messages = build_scrape_messages(brand_id, urls_to_scrape)
        published_count = publish_messages(sqs, queue_url, messages)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "total_seen": len(entries),
            "new_count": len(diff["new"]),
            "changed_count": len(diff["changed"]),
            "unchanged_count": len(diff["unchanged"]),
            "published_count": published_count,
        }),
    }
