#!/usr/bin/env python3
"""
Catalog-wide rescrape for every commercebuild (Storm/Roto Grip/900
Global) product currently missing ALL product_skus rows -- the cleanup
step for the "Tech Sheet" wording fix in
src/commercebuild_product_scraper/app.py's parse_tech_data_pdf_url (see
that function's own "REAL INCIDENT" docstring and DEPLOY_RUNBOOK.md
6f.1).

WHY THIS EXISTS: Al reported product 56897c0b-e3ec-4314-a8dc-238e1b8b7a75
(Storm Tropical Surge Black/Cherry) had zero product_skus despite its
real page clearly showing weight/RG/differential values. Root cause:
parse_tech_data_pdf_url only matched Downloads-section link TEXT
containing "tech data" -- this product's link said "Tech Sheet: Surge
Black/Cherry PDF" instead, so the PDF (the only real source of per-weight
SKU rows for this platform -- see that module's own docstring on why the
page's own flat spec block is cross-check-only, never the SKU source)
never got fetched, and upsert_product's `for sku in pdf_skus:` loop
inserted zero rows. Silent -- no exception, no DLQ entry, a completely
normal-looking products row.

Fixing the parser only prevents this going forward; it doesn't retroactively
fill in product_skus for whatever ALREADY landed with zero rows before the
fix shipped. This script finds every such product (GET /products?
source_platform=commercebuild&missing_skus=true, migration-free -- both
filters already exist on admin_api's list_products, see service.py's own
docstring for missing_skus) and queues a fresh scrape for each, same
GET /products (paginated) + POST /products/{id}/rescrape shape every other
rescrape script in this project uses (see rescrape_netsuite_products.py
and backfill_core_ids.py for the identical pattern and the fuller
Lambda-concurrency-throttle writeup behind the retry/backoff config below
-- unchanged here, this script hits the exact same admin_api and
five-scraper-Lambda-pool shape).

Only ever a subset of products this filter catches will actually have the
"Tech Sheet" wording problem specifically -- missing_skus also matches
retired/archived commercebuild products (a genuine, documented platform
limitation, not a bug -- see COMMERCEBUILD_SCOPING.md) and any other
parse failure. Rescraping those is harmless (upsert_product's own
coalesce-preserve-existing pattern just leaves them with zero SKUs again,
same as today) but won't fix them -- there's currently no way to
distinguish "archived, genuinely no SKU data available" from "current,
wording variant missed" from the product row alone.

This only enqueues the rescrapes; it doesn't wait for them to finish or
verify the resulting SKU rows. Check DEPLOY_RUNBOOK.md 6f for how to
watch CommercebuildProductScraperFunction's logs/DLQ while this drains,
and re-run GET /products?source_platform=commercebuild&missing_skus=true
afterward (or just re-run this script -- idempotent, see run()'s own
docstring) to see how much is left.

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    python3 scripts/rescrape_commercebuild_products.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("rescrape_commercebuild_products")

DEFAULT_PAGE_LIMIT = 200

# Same retry bucket/backoff as backfill_core_ids.py/rescrape_netsuite_
# products.py -- see backfill_core_ids.py's module docstring for the full
# Lambda-concurrency-throttle writeup this guards against (identical
# shape: this script's own rescrape calls fill CommercebuildProductScrape
# Queue, whose Lambda shares the same tiny account-wide concurrency pool
# as AdminApiFunction itself).
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)
RETRY_TOTAL = 5
RETRY_BACKOFF_FACTOR = 1


def get_requests_session():
    """Builds a requests.Session with urllib3 Retry mounted on both
    http/https -- shared by list_commercebuild_products_missing_skus and
    rescrape_product below. A fresh session per call (rather than one
    module-level singleton) keeps this easy to monkeypatch/replace in
    tests."""
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


def list_commercebuild_products_missing_skus(admin_api_url: str, token: str, page_limit: int = DEFAULT_PAGE_LIMIT,
                                              session=None) -> list:
    """Paginates GET /products?source_platform=commercebuild&missing_skus=true
    -- same pagination shape as every other script in this project
    (backfill_core_ids.list_products_missing_core, rescrape_netsuite_
    products.list_netsuite_products). session defaults to a fresh retry-
    enabled one (see get_requests_session) but is overridable so tests can
    inject a fake transport instead of hitting the network."""
    session = session if session is not None else get_requests_session()

    items = []
    offset = 0
    while True:
        resp = session.get(
            f"{admin_api_url}/products",
            params={"source_platform": "commercebuild", "missing_skus": "true", "limit": page_limit, "offset": offset},
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
    other batch script in this project. Idempotent and safe to re-run:
    a product that gets real SKU rows from this rescrape simply stops
    matching missing_skus=true on the next run, and a product that
    genuinely has no obtainable SKU data (an archived/retired page, see
    this module's own docstring) keeps showing up harmlessly -- rescraping
    it again doesn't make things worse, just doesn't fix it either."""
    list_products = list_fn if list_fn is not None else list_commercebuild_products_missing_skus
    rescrape = rescrape_fn if rescrape_fn is not None else rescrape_product

    products = list_products(admin_api_url, token)
    logger.info("Found %d commercebuild product(s) missing all product_skus rows", len(products))

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
