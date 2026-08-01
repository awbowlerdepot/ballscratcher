"""
URL discovery for the NetSuite brand family (MOTIV Bowling to start --
motivbowling.com). See src/netsuite_product_scraper/app.py's module
docstring for how the platform itself was confirmed (SuiteCommerce, not a
guess): a category tile link resolves to
"https://www.motivbowling.com/n_<18-digit-id>", which 302s to a
human-readable canonical URL like
"https://www.motivbowling.com/products/balls/heavy-oil/jackal-onyx.html".
That /n_<id> redirect shape is a NetSuite SuiteCommerce signature, verified
live in a real browser this session (both the tile href and the resulting
canonical URL were read directly off the rendered page and its
window.location after navigating).

Real, confirmed structural facts this module's design rests on (all read
directly off motivbowling.com in a live browser this session, not fetched
by this sandbox's non-browser tools -- see the product scraper's docstring
for why product PAGES specifically can't be fetched without a browser):

1. Two dedicated catalog index pages exist, exactly like Brunswick's
   current/retired split, but here it's two separate URLs rather than one
   sitemap with a path segment:
     - https://www.motivbowling.com/products/balls/  (currently-sold balls)
     - https://www.motivbowling.com/products/balls/retired-balls/  (retired)
   Both are confirmed real (28 balls on the first, 202 on the second as of
   this session) and, importantly, both render their FULL catalog on one
   page with no pagination -- confirmed by finding zero "page/N"-style or
   "Next"-labeled category-level pagination controls on either page.
2. Every ball tile links via a plain, crawlable anchor tag (not a JS
   onclick handler), so this can be parsed by regex the same way the
   WooCommerce/Craft-CMS discovery modules parse plain <a href> links.
   CORRECTED this deploy: the href's raw text is dot-relative
   (href="./n_1094"), not the absolute https://www.motivbowling.com/n_<id>
   form originally documented here -- that was read off a live browser's
   resolved DOM, which normalizes relative hrefs to absolute, so the raw
   form was never actually seen until this deploy's first live smoke
   test. See parse_category_page()'s docstring for the real bug this
   caused and the fix.

This module deliberately does NOT resolve the /n_<id> URL to its canonical
slug URL at discovery time (an extra fetch per product, and product-page
fetches are exactly the ones this session couldn't get working without a
browser -- see the product scraper docstring). It stores the /n_<id> URL
in discovered_urls as-is; requests (used by the product scraper's
fetch_page) follows redirects by default, so the n_id URL works fine as
the stored "url to scrape" without ever needing the canonical form here.

Known, disclosed gap: no sitemap or per-product <lastmod>-equivalent
signal was found for motivbowling.com this session (unlike Brunswick's
sitemap or SWAG's Yoast sitemap), so this can't do the "changed" half of
the new/changed/unchanged diff the other two discovery modules do --
diff_against_known() here only distinguishes new vs. already-known, and
re-touches last_seen_at on every run for known URLs. A previously-known
product whose content changed on motivbowling.com without changing its
URL wouldn't be flagged for re-scrape. Revisit if this matters in
practice (e.g. by hashing page content), not attempted this session.
"""
import json
import logging
import os
import re
from urllib.parse import urlsplit

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_CURRENT_CATEGORY_URL = "https://www.motivbowling.com/products/balls/"
DEFAULT_RETIRED_CATEGORY_URL = "https://www.motivbowling.com/products/balls/retired-balls/"

# NetSuite SuiteCommerce's internal-item-id permalink shape, confirmed real
# (see module docstring). Matches the numeric id out of an href regardless
# of how that href is expressed (dot-relative, root-relative, absolute) --
# see parse_category_page()'s docstring for why extracting just the id and
# rebuilding the URL, rather than resolving the href's path via urljoin,
# is the correct approach here.
PRODUCT_ID_RE = re.compile(r'href="[^"]*?n_(\d+)"')


def fetch_page(url: str, timeout: int = 30) -> str:
    """Fetch raw HTML. Kept separate from parsing so tests can feed real
    fixture HTML without a network call.

    Unverified this session whether a plain requests.get works for these
    category index pages specifically the way it does for Brunswick/SWAG
    -- see the product scraper module docstring for the full session-cookie
    caveat, which applies here too since this hits the same site."""
    import requests

    resp = requests.get(url, headers={"User-Agent": "bowling-scraper/1.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_category_page(html: str, base_url: str) -> set:
    """Returns the set of absolute /n_<id> product URLs linked from a
    category index page.

    Real bug found via this deploy's first live smoke test: the raw HTML
    actually uses a dot-relative href, e.g. href="./n_1094" (confirmed
    via curl against the real retired-balls page), NOT the absolute URL
    the original research assumed. That research read the href directly
    off a live browser's rendered DOM/window.location -- which shows the
    RESOLVED href, not the raw HTML attribute value -- so the dot-relative
    form was never actually seen. A plain urljoin(base_url, './n_1094')
    resolves relative to the category page's own directory, producing a
    broken double-nested URL like
    https://www.motivbowling.com/products/balls/retired-balls/n_1094
    (confirmed real: this 404s) instead of the real, working
    https://www.motivbowling.com/n_1094 (confirmed real: this redirects
    cleanly to the canonical slug page). These /n_<id> permalinks are a
    NetSuite SuiteCommerce convention that's always root-level regardless
    of which category page links to them, so this extracts just the
    numeric id and rebuilds the URL against the site's root directly,
    rather than resolving the href's path via urljoin at all -- correct
    regardless of whether a given href turns out to be dot-relative,
    root-relative, or fully absolute."""
    parts = urlsplit(base_url)
    root = f"{parts.scheme}://{parts.netloc}"
    return {f"{root}/n_{product_id}" for product_id in PRODUCT_ID_RE.findall(html)}


def build_entries(current_urls: set, retired_urls: set) -> list:
    """Combines both category pages into the {url, status} shape
    diff_against_known() expects. A URL appearing on both (shouldn't
    happen based on what was seen this session, but not structurally
    impossible) is treated as current -- the retired-balls page is the
    more deliberate, narrower listing, but "still for sale" is the more
    actionable status if the two ever disagree."""
    entries = [{"url": url, "status": "current"} for url in sorted(current_urls)]
    entries += [{"url": url, "status": "retired"} for url in sorted(retired_urls - current_urls)]
    return entries


def diff_against_known(conn, brand_id: str, entries: list) -> dict:
    """New-vs-known diff against discovered_urls -- see module docstring
    for why this can't also detect "changed" the way the other two
    discovery modules can (no lastmod-equivalent signal available).

    Returns "new"/"unchanged" as lists of the full entry dict (not just the
    bare URL, unlike the other two discovery modules) so status travels
    with each URL -- build_scrape_messages needs it, since the product
    page itself has no reliable status signal of its own (see
    netsuite_product_scraper's module docstring point 1)."""
    new_entries, unchanged_entries = [], []

    with conn.cursor() as cur:
        for entry in entries:
            cur.execute("select id from discovered_urls where url = %s", (entry["url"],))
            row = cur.fetchone()

            if row is None:
                cur.execute(
                    """
                    insert into discovered_urls (brand_id, url, status_path)
                    values (%s, %s, %s)
                    """,
                    (brand_id, entry["url"], entry["status"]),
                )
                new_entries.append(entry)
            else:
                cur.execute(
                    "update discovered_urls set last_seen_at = now() where url = %s",
                    (entry["url"],),
                )
                unchanged_entries.append(entry)
    conn.commit()

    return {"new": new_entries, "changed": [], "unchanged": unchanged_entries}


def build_scrape_messages(brand_id: str, entries: list) -> list:
    """entries is a list of {url, status} dicts (see diff_against_known) --
    status is included in the message body so netsuite_product_scraper can
    write it without needing its own (unreliable) way to derive it."""
    return [json.dumps({"url": e["url"], "brand_id": brand_id, "status": e["status"]}) for e in entries]


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
    current_url = os.environ.get("CURRENT_CATEGORY_URL", DEFAULT_CURRENT_CATEGORY_URL)
    retired_url = os.environ.get("RETIRED_CATEGORY_URL", DEFAULT_RETIRED_CATEGORY_URL)
    brand_id = os.environ["BRAND_ID"]

    logger.info("Discovering current balls from %s", current_url)
    current_urls = parse_category_page(fetch_page(current_url), current_url)
    logger.info("Discovering retired balls from %s", retired_url)
    retired_urls = parse_category_page(fetch_page(retired_url), retired_url)
    logger.info("Found %d current, %d retired product URLs", len(current_urls), len(retired_urls))

    entries = build_entries(current_urls, retired_urls)

    conn = get_db_connection()
    try:
        diff = diff_against_known(conn, brand_id, entries)
    finally:
        conn.close()

    logger.info(
        "Discovery complete: %d new, %d unchanged",
        len(diff["new"]), len(diff["unchanged"]),
    )

    entries_to_scrape = diff["new"] + diff["changed"]
    published_count = 0
    queue_url = os.environ.get("PRODUCT_SCRAPE_QUEUE_URL")
    if entries_to_scrape and queue_url:
        import boto3

        sqs = boto3.client("sqs")
        messages = build_scrape_messages(brand_id, entries_to_scrape)
        published_count = publish_messages(sqs, queue_url, messages)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "total_seen": len(entries),
            "new_count": len(diff["new"]),
            "unchanged_count": len(diff["unchanged"]),
            "published_count": published_count,
        }),
    }
