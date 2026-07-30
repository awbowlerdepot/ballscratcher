"""
URL discovery Lambda.

Fetches a manufacturer's product sitemap, extracts ball product URLs,
classifies them as current/retired from the URL path, and diffs against the
`discovered_urls` table so downstream scraping only processes new or
changed URLs.

Reused across the Craft-CMS brand family (Brunswick, Radical, DV8) by
deploying with SITEMAP_URL/URL_PATH_PATTERN/BRAND_ID pointed at the sister
brand instead of duplicating this function -- see template.yaml. The
Shopify-family brands (Hammer/Ebonite/Track) don't need this function at
all: pull /products.json directly instead of diffing a sitemap.

Kept deliberately split into pure functions (fetch / parse / diff) so the
parsing logic can be unit tested against a real captured sitemap fixture
without a network call or a database -- see tests/test_url_discovery.py.
"""
import json
import logging
import os
import re
import urllib.request
import xml.etree.ElementTree as ET

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

DEFAULT_SITEMAP_URL = "https://brunswickbowling.com/sitemaps-1-section-bowlerProducts-1-sitemap.xml"
DEFAULT_PATH_PATTERN = r"/products/balls/(current|retired)/"


def fetch_sitemap(url: str, timeout: int = 30) -> bytes:
    """Fetch raw sitemap XML bytes. Kept separate from parsing so tests can
    feed real captured fixture bytes without a network call."""
    req = urllib.request.Request(url, headers={"User-Agent": "bowling-scraper/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_sitemap(xml_bytes: bytes, path_pattern: str = DEFAULT_PATH_PATTERN) -> list:
    """Parse sitemap XML into a list of {url, status, lastmod} dicts, kept
    to only the URLs matching path_pattern (ball product pages -- excludes
    apparel/bags/accessories/etc. that live in the same sitemap file).

    status is 'current' or 'retired', inferred from the URL path -- Brunswick
    (and the sister Craft-CMS brands) put that directly in the URL, so this
    doesn't need a page fetch to classify.

    lastmod is optional per the sitemap spec -- some entries won't have one,
    handled as None rather than raising.
    """
    pattern = re.compile(path_pattern)
    root = ET.fromstring(xml_bytes)

    results = []
    for url_el in root.findall("sm:url", SITEMAP_NS):
        loc_el = url_el.find("sm:loc", SITEMAP_NS)
        if loc_el is None or not loc_el.text:
            continue
        loc = loc_el.text.strip()

        match = pattern.search(loc)
        if not match:
            continue

        lastmod_el = url_el.find("sm:lastmod", SITEMAP_NS)
        lastmod = lastmod_el.text.strip() if lastmod_el is not None and lastmod_el.text else None

        results.append({
            "url": loc,
            "status": match.group(1),  # 'current' or 'retired'
            "lastmod": lastmod,
        })

    return results


def diff_against_known(conn, brand_id: str, entries: list) -> dict:
    """Upsert entries into discovered_urls, returning which ones are new vs.
    changed (lastmod moved) vs. unchanged.

    `conn` is a psycopg2-style connection, passed in rather than opened here
    so this is testable with a fake/mock connection independent of
    parse_sitemap and independent of real AWS/DB access.
    """
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


def get_db_connection():
    """Deferred import + Secrets Manager lookup, so the parse_sitemap/diff
    logic above can be unit tested without psycopg2 or AWS credentials
    available in the test environment."""
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
    sitemap_url = os.environ.get("SITEMAP_URL", DEFAULT_SITEMAP_URL)
    path_pattern = os.environ.get("URL_PATH_PATTERN", DEFAULT_PATH_PATTERN)
    brand_id = os.environ["BRAND_ID"]  # set per-deployment -- identifies the brand row in Postgres

    logger.info("Fetching sitemap %s", sitemap_url)
    xml_bytes = fetch_sitemap(sitemap_url)
    entries = parse_sitemap(xml_bytes, path_pattern)
    logger.info("Parsed %d ball product URLs from sitemap", len(entries))

    conn = get_db_connection()
    try:
        diff = diff_against_known(conn, brand_id, entries)
    finally:
        conn.close()

    logger.info(
        "Discovery complete: %d new, %d changed, %d unchanged",
        len(diff["new"]), len(diff["changed"]), len(diff["unchanged"]),
    )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "total_seen": len(entries),
            "new_count": len(diff["new"]),
            "changed_count": len(diff["changed"]),
            "unchanged_count": len(diff["unchanged"]),
            "new_urls": diff["new"],
            "changed_urls": diff["changed"],
        }),
    }
