#!/usr/bin/env python3
"""
Catalog-wide rescrape for every netsuite-platform (MOTIV) product -- the
cleanup step for the image-scoping fix in src/netsuite_product_scraper/
app.py (see that module's docstring point 7, "SECOND real bug", and
DEPLOY_RUNBOOK.md 6e.6).

WHY THIS EXISTS: parse_images() used to sweep the entire raw page HTML for
any background-image url(...) under userfiles/filemanager, which wrongly
attached other products' photos (most likely from a cross-sell/"related
products" strip present on every product page) to whichever product
happened to be scraped. The fix scopes parsing to the real gallery/
carousel containers, so any FUTURE scrape is correct -- but every MOTIV
product already scraped under the old logic may still have those extra/
wrong product_images rows sitting in the DB right now. Unlike the status-
clobber fix (scripts/backfill_netsuite_status.py), there's no reliable way
to distinguish a wrongly-attached image row from a real one after the
fact from data alone -- both look like ordinary product_images rows --
so a targeted SQL correction isn't possible here. The only fix is a fresh
scrape of every affected product under the corrected parser, which
naturally replaces (see upsert_product's ON CONFLICT handling in
netsuite_product_scraper/app.py) whatever image rows it parses this time.

This is the same "rescrape every product matching a filter" shape as
scripts/backfill_core_ids.py -- same GET /products (paginated) + POST
/products/{id}/rescrape (via admin_api's existing queue_rescrape, see
service.py) pattern, just scoped by the new source_platform filter
(GET /products?source_platform=netsuite -- see service.list_products'
docstring for why source_platform rather than brand_id: it's the more
honest "every product this specific scraper touches" filter, robust to
NetSuite someday covering more than just MOTIV) instead of missing_core.
See backfill_core_ids.py's own module docstring for the fuller reasoning
behind the retry/backoff config and the real Lambda-concurrency-throttle
incident it guards against -- unchanged here, this script hits the exact
same admin_api and five-scraper-Lambda-pool shape.

This only enqueues the rescrapes; it doesn't wait for them to finish or
verify the resulting images. Check DEPLOY_RUNBOOK.md 6e for how to watch
NetsuiteProductScraperFunction's logs/DLQ while this drains, and spot-check
a few product detail pages in the admin UI afterward.

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    python3 scripts/rescrape_netsuite_products.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("rescrape_netsuite_products")

DEFAULT_PAGE_LIMIT = 200

# Same retry bucket/backoff as backfill_core_ids.py -- see that script's
# module docstring for the full Lambda-concurrency-throttle writeup this
# guards against (identical shape: this script's own rescrape calls fill
# NetsuiteProductScrapeQueue, whose Lambda shares the same tiny account-
# wide concurrency pool as AdminApiFunction itself).
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)
RETRY_TOTAL = 5
RETRY_BACKOFF_FACTOR = 1


def get_requests_session():
    """Builds a requests.Session with urllib3 Retry mounted on both
    http/https -- shared by list_netsuite_products and rescrape_product
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


def list_netsuite_products(admin_api_url: str, token: str, page_limit: int = DEFAULT_PAGE_LIMIT,
                            session=None) -> list:
    """Paginates GET /products?source_platform=netsuite -- same pagination
    shape as every other script in this project (backfill_core_ids.
    list_products_missing_core, backfill_video_review_rollups.
    list_products_needing_refresh). session defaults to a fresh retry-
    enabled one (see get_requests_session) but is overridable so tests can
    inject a fake transport instead of hitting the network."""
    session = session if session is not None else get_requests_session()

    items = []
    offset = 0
    while True:
        resp = session.get(
            f"{admin_api_url}/products",
            params={"source_platform": "netsuite", "limit": page_limit, "offset": offset},
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
    lists already has source_platform='netsuite', which always resolves to
    a real queue env var per SCRAPE_QUEUE_ENV_VAR_BY_PLATFORM) -- still
    handled the same as backfill_core_ids.py rather than assumed away, in
    case NetsuiteProductScrapeQueue's env var itself isn't configured on a
    given deployment."""
    list_products = list_fn if list_fn is not None else list_netsuite_products
    rescrape = rescrape_fn if rescrape_fn is not None else rescrape_product

    products = list_products(admin_api_url, token)
    logger.info("Found %d MOTIV/netsuite product(s) to rescrape", len(products))

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
