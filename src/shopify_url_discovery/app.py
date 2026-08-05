"""
URL discovery for the Shopify brand family (Hammer Bowling to start --
hammerbowling.com, confirmed live via direct fetch this session: a
meta-shopify-y tag, /cdn/shop/ CDN image URLs, and a "Powered by Shopify"
footer link).

Deliberately NOT a sitemap-diff (Craft-CMS family) or a scraped-HTML-
listing-page (WooCommerce family) approach. Shopify exposes a public,
unauthenticated JSON endpoint for every collection --
GET /collections/{handle}/products.json -- confirmed real this session
against hammerbowling.com, and it returns exactly the product set the
merchandised collection page shows, already excluding non-ball collections
(bags, shoes, accessories, pro-staff bios) for free, because collection
membership on this platform IS the site's own real ball/non-ball split.
No UrlPathPattern-style regex filtering needed at all -- confirmed real
this session that the unfiltered all-products feed (/products.json) mixes
bags/shoes/pro-staff-bio "products" in with no distinguishing URL segment;
using the per-collection endpoint sidesteps that entirely rather than
filtering it out after the fact the way UrlPathPattern does for Brunswick.

Current vs. retired status is also a collection-membership question here,
not a URL-path segment (unlike Brunswick's /products/balls/(current|
retired)/) or an on-page attribute (unlike SWAG's Production-status
field): retired balls live under their own real collection,
https://hammerbowling.com/collections/retired-balls (confirmed live --
linked from the footer's "RETIRED BALLS" nav item and from the /pages/
retired-balls landing page's "View all" link), separate from the five
current-ball performance-tier collections (high-performance,
upper-mid-performance, mide-performance [a real, confirmed typo on
Hammer's own site -- not a bug here, see DEFAULT_COLLECTION_HANDLES],
lower-mid-performance, polyester). A product seen under retired-balls and
never under a current-tier collection is retired; a product seen under any
current-tier collection is current even if it also happens to still be
indexed under retired-balls (current wins on conflict -- not observed live
this session across any real product, but a safe default: it more likely
means "back in production" than a stale retired-collection index entry
that should suppress a real current listing).

Each product's own `updated_at` timestamp (present directly on every
products.json entry) stands in for the sitemap <lastmod> value the
Craft-CMS/WooCommerce/NetSuite families use for their new/changed/
unchanged diff -- reusing the same discovered_urls.sitemap_lastmod column
and diff_against_known() shape rather than a schema change, since it
serves the exact same "did this change since we last saw it" purpose here,
just sourced from a different field. Unlike WooCommerce's version (see
woocommerce_url_discovery/app.py), status_path IS populated here: collection
membership gives a real, confirmed current/retired signal at discovery
time, same as Brunswick's URL-path version -- see
shopify_product_scraper/app.py's module docstring for why that matters
(this platform's product page itself doesn't reliably expose current vs.
retired, so the scraper reads status_path back from this table instead of
re-deriving it).

Three brands confirmed sharing this platform: Hammer (confirmed prior
session), and Track (trackbowling.com) + Ebonite (ebonite.com), both
confirmed live this session via the same signals (collections.json,
retired-balls as a real separate collection, /products/{handle}.json).
STORE_DOMAIN and COLLECTION_HANDLES are both env-var driven for exactly
this reason, same reuse convention as RadicalUrlDiscoveryFunction/
Dv8UrlDiscoveryFunction reusing url_discovery/app.py's code with different
SitemapUrl/BrandId params -- TrackUrlDiscoveryFunction/
EboniteUrlDiscoveryFunction in template.yaml are each a second/third
instance of this same CodeUri, not a rewrite.

Collection-handle sets are NOT identical across the three brands --
confirmed live, each store's own collections.json was fetched and checked
directly rather than assumed: Track has no lower-mid-performance
collection at all (high-performance/upper-mid-performance/mid-performance/
polyester/retired-balls only), while Ebonite has no upper-mid-performance
collection but does have a pro-performance tier Hammer/Track don't
(pro-performance/high-performance/mid-performance/lower-mid-performance/
polyester/retired-balls) -- see TrackCollectionHandles/
EboniteCollectionHandles in template.yaml for the exact confirmed sets.
Also note Track spells its mid-tier collection "mid-performance"
correctly, unlike Hammer's confirmed "mide-performance" typo -- copying
Hammer's DEFAULT_COLLECTION_HANDLES verbatim for Track would silently
0-discover its mid-tier ball.
"""
import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_STORE_DOMAIN = "hammerbowling.com"
DEFAULT_COLLECTION_HANDLES = (
    "high-performance,upper-mid-performance,mide-performance,"
    "lower-mid-performance,polyester,retired-balls"
)
RETIRED_COLLECTION_HANDLES = {"retired-balls"}

PAGE_LIMIT = 250  # Shopify's own max page size for this endpoint
MAX_PAGES = 20  # safety cap; Hammer's largest collection (retired-balls) is nowhere near 5000 products


def fetch_collection_page(store_domain: str, handle: str, page: int, limit: int = PAGE_LIMIT, timeout: int = 30) -> dict:
    """Fetch one page of a collection's products.json. Kept separate from
    the pagination loop so tests can feed real fixture JSON without a
    network call."""
    import requests

    url = f"https://{store_domain}/collections/{handle}/products.json"
    resp = requests.get(
        url,
        params={"limit": limit, "page": page},
        headers={"User-Agent": "bowling-scraper/1.0"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def discover_collection_products(fetch_fn, store_domain: str, handle: str, limit: int = PAGE_LIMIT, max_pages: int = MAX_PAGES) -> list:
    """Paginates a single collection until a short (< limit) page comes
    back -- same short-page-means-last-page signal
    scripts/backfill_core_ids.py's own pagination already relies on for the
    admin API. Returns the raw product dicts (not yet reduced to
    {url, lastmod}) so the caller can still tell which collection each one
    came from, needed for the current/retired classification in
    build_entries()."""
    products = []
    page = 1
    while page <= max_pages:
        data = fetch_fn(store_domain, handle, page, limit)
        batch = data.get("products", [])
        products.extend(batch)
        if len(batch) < limit:
            break
        page += 1
    return products


def build_entries(store_domain: str, products_by_handle: dict) -> list:
    """Reduces {collection_handle: [product dict, ...]} into the
    {url, lastmod, status} shape diff_against_known() expects, applying
    the current-wins-over-retired classification described in the module
    docstring. Collapses to one entry per distinct product handle even if
    a product turned up in more than one collection dict."""
    status_by_handle = {}
    updated_at_by_handle = {}

    for handle, products in products_by_handle.items():
        is_retired_collection = handle in RETIRED_COLLECTION_HANDLES
        for product in products:
            slug = product["handle"]
            updated_at_by_handle[slug] = product.get("updated_at")
            if is_retired_collection:
                status_by_handle.setdefault(slug, "retired")
            else:
                status_by_handle[slug] = "current"  # current always wins, see docstring

    entries = [
        {
            "url": f"https://{store_domain}/products/{slug}",
            "lastmod": updated_at_by_handle.get(slug),
            "status": status,
        }
        for slug, status in status_by_handle.items()
    ]
    return sorted(entries, key=lambda e: e["url"])


def diff_against_known(conn, brand_id: str, entries: list) -> dict:
    """Same new/changed/unchanged diff behavior as url_discovery.
    diff_against_known and woocommerce_url_discovery.diff_against_known
    against the same discovered_urls table -- duplicated rather than
    imported cross-module, see product_scraper/app.py's publish_messages
    docstring for why (each Lambda here is its own independent deployment
    package)."""
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
                    insert into discovered_urls (brand_id, url, status_path, sitemap_lastmod)
                    values (%s, %s, %s, %s)
                    """,
                    (brand_id, entry["url"], entry["status"], entry["lastmod"]),
                )
                new_urls.append(entry["url"])
            else:
                existing_lastmod = row[0]
                incoming_lastmod = entry["lastmod"]
                if incoming_lastmod and str(existing_lastmod) != incoming_lastmod:
                    cur.execute(
                        """
                        update discovered_urls
                        set sitemap_lastmod = %s, status_path = %s, last_seen_at = now(), scrape_status = 'pending'
                        where url = %s
                        """,
                        (incoming_lastmod, entry["status"], entry["url"]),
                    )
                    changed_urls.append(entry["url"])
                else:
                    cur.execute(
                        "update discovered_urls set status_path = %s, last_seen_at = now() where url = %s",
                        (entry["status"], entry["url"]),
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
    store_domain = os.environ.get("STORE_DOMAIN", DEFAULT_STORE_DOMAIN)
    handles = [
        h.strip()
        for h in os.environ.get("COLLECTION_HANDLES", DEFAULT_COLLECTION_HANDLES).split(",")
        if h.strip()
    ]
    brand_id = os.environ["BRAND_ID"]

    products_by_handle = {}
    for handle in handles:
        logger.info("Discovering products in collection %s", handle)
        products_by_handle[handle] = discover_collection_products(fetch_collection_page, store_domain, handle)
        logger.info("Found %d products in %s", len(products_by_handle[handle]), handle)

    entries = build_entries(store_domain, products_by_handle)

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
