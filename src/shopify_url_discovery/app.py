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
directly rather than assumed. Ebonite has no upper-mid-performance
collection but does have a pro-performance tier Hammer/Track don't
(pro-performance/high-performance/mid-performance/lower-mid-performance/
polyester/retired-balls) -- see EboniteCollectionHandles in template.yaml
for the exact confirmed set, still accurate as of this writing (its
tier-handle sum matches its own "balls" collection's product_count
exactly, a live cross-check worth re-running if Ebonite is ever reported
missing balls the way Track was below).

REAL INCIDENT: Track's own per-tier collections (high-performance/
upper-mid-performance/mid-performance) were confirmed live and correct
when Track was first onboarded, but Track later reorganized its site and
those tier collections all now report product_count: 0 on
trackbowling.com/collections.json -- everything current lives under one
real collection instead (handle "balls", matching what Ebonite's own
collections.json literally titles "Current Ball Lineup" for the
equivalent collection). Al reported "track is missing balls" on the
admin site; refetching collections.json live confirmed only Track's
"polyester" handle (1 product) was still resolving under the old
tier-handle config, so TrackUrlDiscoveryFunction was silently discovering
1 of Track's ~7 current balls. TrackCollectionHandles' default in
template.yaml (and the matching samconfig.toml override, the value that
actually took effect) were updated to "balls,retired-balls" -- no code
change needed here, build_entries() below already treats any handle
outside RETIRED_COLLECTION_HANDLES as a current-tier signal, this was
purely a stale per-brand config value. Worth remembering for Hammer/
Ebonite too: collection-handle sets are a live merchandising decision on
each brand's own site, not a stable platform contract -- confirm-live-
don't-assume applies to re-checking these later, not just the initial
onboarding.
"""
import datetime
import hashlib
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


def product_fingerprint(product: dict) -> str:
    """sha256 of exactly what shopify_product_scraper reads from a product
    (runbook 6ea): title, body_html (specs, description, RG/diff all live
    in it), product_type, tags, and the images' position/src. A changed
    image re-uploads with a new src (Shopify's ?v= version), so image
    changes are caught. Deliberately NOT included: updated_at (Shopify
    re-stamps it store-wide several times a day -- the reason this exists)
    and variants (stock/price churn; the scraper parses SKUs/weights from
    body_html, not variants)."""
    tags = product.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    images = sorted(
        ([img.get("position") or 0, img.get("src") or ""] for img in product.get("images") or []),
        key=lambda pair: (pair[0], pair[1]),
    )
    material = {
        "title": product.get("title") or "",
        "body_html": product.get("body_html") or "",
        "product_type": product.get("product_type") or "",
        "tags": sorted(tags),
        "images": images,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# RETIRED-TAGGED SWEEP (runbook 6ec). Al asked what it would take to place
# Hammer Special Effect from the March Brunswick chart (oil 11, motion 15,
# unmatched since 6ch). Hammer sells it, but files it in none of the
# collections above: it's only in the store-wide "all" collection, tagged
# "Retired", with an empty product_type -- not in Hammer's own retired-balls
# collection. Four more retired Hammer balls are filed the same way
# (Hammerhead, Ocean Vibe, RAW Hammer Black and Blue/Silver/White), so none
# of the five had ever been scraped. Track and Ebonite have no such orphans
# today. Every run now also reads "all" and adds any product that
# (a) none of the configured collections returned, (b) is tagged
# "Retired", and (c) has a ball-ish product_type ('' or "Bowling Ball").
# Shoes, bags and accessories carry their own product_type; pro-staff and
# collegiate pages are typed "Pro Staff"/"Bowler"/"Collegiate Bowling" or
# tagged as staffers, so they're excluded. They go in under the pseudo
# collection RETIRED_TAGGED_HANDLE, which build_entries classifies as
# retired because it starts with "retired".
ALL_COLLECTION_HANDLE = "all"
RETIRED_TAGGED_HANDLE = "retired-tagged-in-all"
BALL_PRODUCT_TYPES = {"", "bowling ball", "bowling balls", "ball", "balls"}
STAFF_TAGS = {"national staffer", "international staffer", "bowler", "collegiate bowling staff"}


def retired_tagged_orphans(all_products: list, already_seen: set) -> list:
    """Products from the "all" collection that RETIRED-TAGGED SWEEP adds."""
    out = []
    for product in all_products:
        if product.get("handle") in already_seen:
            continue
        tags = product.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        lowered = {t.strip().lower() for t in tags}
        if "retired" not in lowered or lowered & STAFF_TAGS:
            continue
        if (product.get("product_type") or "").strip().lower() not in BALL_PRODUCT_TYPES:
            continue
        out.append(product)
    return out


def build_entries(store_domain: str, products_by_handle: dict) -> list:
    """Reduces {collection_handle: [product dict, ...]} into the
    {url, lastmod, status} shape diff_against_known() expects, applying
    the current-wins-over-retired classification described in the module
    docstring. Collapses to one entry per distinct product handle even if
    a product turned up in more than one collection dict."""
    status_by_handle = {}
    updated_at_by_handle = {}
    fingerprint_by_handle = {}

    for handle, products in products_by_handle.items():
        # Columbia 300's is "retired-balls-1" (Shopify appends -1 to a
        # duplicate handle) -- runbook 6dn. An exact-set check would have
        # marked all 392 of its balls CURRENT; any handle starting with
        # "retired" is the retired collection.
        is_retired_collection = handle in RETIRED_COLLECTION_HANDLES or handle.startswith("retired")
        for product in products:
            slug = product["handle"]
            updated_at_by_handle[slug] = product.get("updated_at")
            fingerprint_by_handle[slug] = product_fingerprint(product)
            if is_retired_collection:
                status_by_handle.setdefault(slug, "retired")
            else:
                status_by_handle[slug] = "current"  # current always wins, see docstring

    entries = [
        {
            "url": f"https://{store_domain}/products/{slug}",
            "lastmod": updated_at_by_handle.get(slug),
            "fingerprint": fingerprint_by_handle.get(slug),
            "status": status,
        }
        for slug, status in status_by_handle.items()
    ]
    return sorted(entries, key=lambda e: e["url"])



def lastmod_changed(existing, incoming) -> bool:
    """Has this URL's lastmod moved? Compares instants, not strings
    (runbook 6dx). REAL INCIDENT, found 2026-10-17: this used to be
    `str(existing_lastmod) != incoming_lastmod`, but psycopg2 hands back
    discovered_urls.sitemap_lastmod as a timezone-aware datetime
    ("2025-07-15 13:14:20+00:00") while the sitemap/feed gives ISO 8601 in
    the site's own offset ("2025-07-15T09:14:20-04:00"). Those strings
    never match, so every URL counted as "changed" on every run:
    "0 new, 260 changed, 0 unchanged" for Brunswick, and the same for
    Radical, DV8, Hammer, Track, Ebonite and Swag (about 1,100 pointless
    scrapes a day). Pages the manufacturer's site 500s on landed in the
    DLQ daily. Al picked "Fix + weekly full": only new/changed URLs daily,
    plus a weekly {"full_refresh": true} run as a safety net for edits made
    without a lastmod bump.

    No incoming lastmod -> not changed (same as before). A stored NULL with
    an incoming value -> changed. An unparseable value falls back to the
    old string comparison rather than raising.
    """
    if not incoming:
        return False
    if existing is None:
        return True

    def as_instant(value):
        if isinstance(value, datetime.datetime):
            dt = value
        else:
            dt = datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)

    try:
        return as_instant(existing) != as_instant(incoming)
    except ValueError:
        return str(existing) != incoming

def diff_against_known(conn, brand_id: str, entries: list) -> dict:
    """Same new/changed/unchanged diff as url_discovery's and
    woocommerce_url_discovery's, against the same discovered_urls table --
    duplicated rather than imported cross-module (each Lambda here is its
    own deployment package).

    Change detection (runbook 6ea): an entry with a "fingerprint" (every
    real Shopify entry, see product_fingerprint) is changed only when its
    fingerprint differs from the stored content_fingerprint. A row with no
    stored fingerprint yet just records one as its baseline and counts as
    unchanged -- the Sunday full re-scrape (6dx) covers anything that
    changed before the baseline. sitemap_lastmod is still stored, for
    reference only. An entry without a fingerprint (not produced by
    build_entries today) falls back to lastmod_changed."""
    new_urls, changed_urls, unchanged_urls = [], [], []

    with conn.cursor() as cur:
        for entry in entries:
            cur.execute(
                "select sitemap_lastmod, content_fingerprint from discovered_urls where url = %s",
                (entry["url"],),
            )
            row = cur.fetchone()
            incoming_fp = entry.get("fingerprint")

            if row is None:
                cur.execute(
                    """
                    insert into discovered_urls (brand_id, url, status_path, sitemap_lastmod, content_fingerprint)
                    values (%s, %s, %s, %s, %s)
                    """,
                    (brand_id, entry["url"], entry["status"], entry["lastmod"], incoming_fp),
                )
                new_urls.append(entry["url"])
                continue

            existing_lastmod, existing_fp = row[0], row[1]
            if incoming_fp is not None:
                changed = existing_fp is not None and existing_fp != incoming_fp
            else:
                changed = lastmod_changed(existing_lastmod, entry["lastmod"])

            if changed:
                cur.execute(
                    """
                    update discovered_urls
                    set sitemap_lastmod = %s, content_fingerprint = %s, status_path = %s,
                        last_seen_at = now(), scrape_status = 'pending'
                    where url = %s
                    """,
                    (entry["lastmod"], incoming_fp, entry["status"], entry["url"]),
                )
                changed_urls.append(entry["url"])
            else:
                cur.execute(
                    """
                    update discovered_urls
                    set status_path = %s, sitemap_lastmod = coalesce(%s, sitemap_lastmod),
                        content_fingerprint = coalesce(%s, content_fingerprint), last_seen_at = now()
                    where url = %s
                    """,
                    (entry["status"], entry["lastmod"], incoming_fp, entry["url"]),
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

    # RETIRED-TAGGED SWEEP (runbook 6ec). Its own try: a hiccup reading
    # "all" must never block the normal collections' discovery.
    try:
        seen = {p["handle"] for products in products_by_handle.values() for p in products}
        all_products = discover_collection_products(fetch_collection_page, store_domain, ALL_COLLECTION_HANDLE)
        orphans = retired_tagged_orphans(all_products, seen)
        if orphans:
            products_by_handle[RETIRED_TAGGED_HANDLE] = orphans
        logger.info("Retired-tagged sweep: %d product(s) only in '%s' (%s)", len(orphans), ALL_COLLECTION_HANDLE,
                    ", ".join(p["handle"] for p in orphans) or "none")
    except Exception:
        logger.exception("Retired-tagged sweep of '%s' failed; continuing with the configured collections",
                         ALL_COLLECTION_HANDLE)

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
    # Weekly safety net (runbook 6dx): {"full_refresh": true} re-queues every
    # URL still in the feed, the unchanged ones too, so an edit the site
    # made without moving lastmod is still picked up within a week.
    if (event or {}).get("full_refresh"):
        urls_to_scrape = urls_to_scrape + diff["unchanged"]
        logger.info("Full refresh: also queueing %d unchanged URL(s)", len(diff["unchanged"]))
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
