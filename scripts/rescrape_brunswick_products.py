#!/usr/bin/env python3
"""
Catalog-wide rescrape for every craft_cms-platform product (Brunswick,
plus Radical/DV8 -- one shared scraper/queue, see
src/admin_api/service.py's SCRAPE_QUEUE_ENV_VAR_BY_PLATFORM comment) --
the cleanup step for the stale-image DELETE + S3 orphan cleanup fix in
src/product_scraper/app.py, ported from netsuite_product_scraper (MOTIV)
per Al's direct ask ("brunswick needs an image cleanup like motiv did" --
see DEPLOY_RUNBOOK.md's "Stale-image DELETE + S3 orphan cleanup ported
from MOTIV" writeup). Exact mirror of scripts/rescrape_netsuite_products.py,
just scoped by source_platform='craft_cms' instead of 'netsuite'.

WHY THIS EXISTS: upsert_product's old image-upsert logic (INSERT ... ON
CONFLICT (product_id, source_url) DO UPDATE) only ever inserted a row for
a source_url still on the page or updated one that already matched -- it
never deleted a row for a photo the current parse no longer found. So any
Brunswick/Radical/DV8 product already scraped under the old logic may
still have stray/stale product_images rows sitting in the DB right now
(and any already-mirrored S3 objects for them are still orphaned). The
fix (upsert_product's new stale-image DELETE + delete_orphaned_image_
objects) only changes what a FUTURE scrape does -- it can't retroactively
clean up rows from scrapes that already happened. The only way to clear
those out is a fresh scrape of every affected product under the corrected
code, which naturally replaces whatever image rows it parses this time
(see upsert_product's own docstring).

Same "rescrape every product matching a filter" shape as
scripts/backfill_core_ids.py and scripts/rescrape_netsuite_products.py --
GET /products (paginated) + POST /products/{id}/rescrape (via admin_api's
existing queue_rescrape, see service.py), scoped by the source_platform
filter (GET /products?source_platform=craft_cms -- see
service.list_products' docstring for why source_platform rather than
brand_id: it's the more honest "every product this specific scraper
touches" filter, and craft_cms already covers Brunswick/Radical/DV8
together since they share one ProductScraperFunction/ProductScrapeQueue).
See backfill_core_ids.py's own module docstring for the fuller reasoning
behind the retry/backoff config and the real Lambda-concurrency-throttle
incident it guards against -- unchanged here, this script hits the exact
same admin_api and five-scraper-Lambda-pool shape.

This only enqueues the rescrapes; it doesn't wait for them to finish or
verify the resulting images. Check DEPLOY_RUNBOOK.md's Brunswick pipeline
section for how to watch ProductScraperFunction's logs/DLQ while this
drains, and spot-check a few product detail pages in the admin UI
afterward.

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    python3 scripts/rescrape_brunswick_products.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("rescrape_brunswick_products")

DEFAULT_PAGE_LIMIT = 200

# Same retry bucket/backoff as backfill_core_ids.py -- see that script's
# module docstring for the full Lambda-concurrency-throttle writeup this
# guards against (identical shape: this script's own rescrape calls fill
# ProductScrapeQueue, whose Lambda shares the same tiny account-wide
# concurrency pool as AdminApiFunction itself).
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)
RETRY_TOTAL = 5
RETRY_BACKOFF_FACTOR = 1


def get_requests_session():
    """Builds a requests.Session with urllib3 Retry mounted on both
    http/https -- shared by list_craft_cms_products and rescrape_product
    below. A fresh session per call (rather than one module-level
    singleton) keeps this easy to monkeypatch/replace in tests."""
    import requests
    from urllib3.util.retry import Retry

    session = requests.Session()
    retry = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUS_FORCELIST,
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def list_craft_cms_products(admin_api_url: str, token: str, page_limit: int = DEFAULT_PAGE_LIMIT,
                             session=None) -> list:
    """Paginates GET /products?source_platform=craft_cms -- same
    pagination shape as every other script in this project (backfill_
    core_ids.list_products_missing_core, rescrape_netsuite_products.
    list_netsuite_products). session defaults to a fresh retry-enabled one
    (see get_requests_session) but is overridable so tests can inject a
    fake transport instead of hitting the network."""
    session = session if session is not None else get_requests_session()

    items = []
    offset = 0
    while True:
        resp = session.get(
            f"{admin_api_url}/products",
            params={"source_platform": "craft_cms", "limit": page_limit, "offset": offset},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        resp.raise_for_status()
        page = resp.json().get("items", [])
        items.extend(page)
        if len(page) < page_limit:
            break
        offset += page_limit
    return items


def rescrape_product(admin_api_url: str, token: str, product_id: str, session=None) -> dict:
    session = session if session is not None else get_requests_session()

    resp = session.post(
        f"{admin_api_url}/products/{product_id}/rescrape",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def run(admin_api_url: str, token: str, list_fn=None, rescrape_fn=None) -> dict:
    """Tolerates per-product errors -- one bad URL or a transient SQS
    hiccup shouldn't stop the rest of the batch, same principle as every
    other batch script in this project. queue_rescrape's {"queued": False,
    ...} shouldn't actually occur here in practice (every product this
    lists already has source_platform='craft_cms', which always resolves
    to a real queue env var per SCRAPE_QUEUE_ENV_VAR_BY_PLATFORM) -- still
    handled the same as backfill_core_ids.py/rescrape_netsuite_products.py
    rather than assumed away, in case ProductScrapeQueue's env var itself
    isn't configured on a given deployment."""
    list_products = list_fn if list_fn is not None else list_craft_cms_products
    rescrape = rescrape_fn if rescrape_fn is not None else rescrape_product

    products = list_products(admin_api_url, token)
    logger.info("Found %d Brunswick/Radical/DV8 (craft_cms) product(s) to rescrape", len(products))

    queued = 0
    skipped = 0
    errors = 0
    for product in products:
        product_id = product["id"]
        try:
            result = rescrape(admin_api_url, token, product_id)
            if result.get("queued"):
                queued += 1
                logger.info("Queued rescrape for product_id=%s name=%r", product_id, product.get("name"))
            else:
                skipped += 1
                logger.info("product_id=%s name=%r: %s", product_id, product.get("name"), result.get("reason"))
        except Exception:
            errors += 1
            logger.exception("Failed to queue rescrape for product_id=%s name=%r -- will retry next run",
                              product_id, product.get("name"))

    return {"total": len(products), "queued": queued, "skipped": skipped, "errors": errors}


def main():
    admin_api_url = os.environ.get("ADMIN_API_URL")
    token = os.environ.get("ADMIN_API_TOKEN")
    if not admin_api_url or not token:
        logger.error("ADMIN_API_URL and ADMIN_API_TOKEN must both be set -- see this script's module docstring for setup.")
        sys.exit(1)

    summary = run(admin_api_url, token)
    logger.info("Done: %s", summary)

    if summary["total"] > 0 and summary["queued"] == 0 and summary["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
