#!/usr/bin/env python3
"""
Backfill helper for the coverstocks table (migration 008/009 -- see
src/admin_api/service.py's list_products missing_coverstock filter and
each *_product_scraper's get_or_create_coverstock_id). Exact mirror of
scripts/backfill_core_ids.py -- see that script's docstring for the full
reasoning (queue_rescrape, SCRAPE_QUEUE_ENV_VAR_BY_PLATFORM, retry/
throttle behavior); only the field name and admin_api filter differ.

WHY THIS EXISTS: unlike migration 008 itself (which backfilled
coverstock_id for every already-scraped product in one shot, since
coverstock_name/material/type already existed as real data on every
products row -- see 008's own comment), a product that's missing a
coverstock_id *now* is missing one because its own coverstock_name was
never captured on its last scrape (or the manufacturer's page didn't have
one at scrape time), not because 008 skipped it. That only gets fixed by
actually re-fetching and re-parsing the page -- nothing else re-triggers a
scrape for an already-known product on its own.

REAL EXAMPLE that prompted this script (Al, 2026-08-07): a live Hammer
product (Raw Hammer - Black/Grey) was missing its own coverstock_id, and
the "Juiced Solid" coverstocks row it should have mapped to had a null
material. Investigation confirmed today's live page parses correctly
end-to-end (coverstock_name="Juiced Solid", material=reactive_resin,
type=solid via parse_coverstock) -- no scraper bug. The product's own
`updated_at` from Shopify was today's date, meaning the page was edited
recently and this product hasn't been rescraped since. This script (via
GET /products?missing_coverstock=true + POST /products/{id}/rescrape) is
the fix for that class of problem: it re-scrapes every product currently
missing a coverstock_id, which both (a) sets that product's own
coverstock_id via get_or_create_coverstock_id, and (b) -- for free, via
that same function's `coalesce(coverstocks.material, excluded.material)`
on conflict -- backfills material/type onto a shared coverstocks row like
"Juiced Solid" if some earlier scrape created it without either, exactly
the "Juiced Solid" symptom Al reported. No separate migration or manual
UPDATE needed for that half of the bug; a fresh scrape resolves both at
once.

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    python3 scripts/backfill_coverstock_ids.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_coverstock_ids")

DEFAULT_PAGE_LIMIT = 200

# Same retry posture as backfill_core_ids.py -- see that script's
# module docstring for the full story on the 503-from-Lambda-throttle
# issue this guards against.
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)
RETRY_TOTAL = 5
RETRY_BACKOFF_FACTOR = 1


def get_requests_session():
    """Builds a requests.Session with urllib3 Retry mounted on both
    http/https -- see backfill_core_ids.get_requests_session's docstring
    (identical implementation, kept separate per-script rather than
    shared so each backfill script stays a single self-contained file,
    matching this project's existing scripts/ convention)."""
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


def list_products_missing_coverstock(admin_api_url: str, token: str, page_limit: int = DEFAULT_PAGE_LIMIT,
                                      session=None) -> list:
    """Paginates GET /products?missing_coverstock=true. See
    backfill_core_ids.list_products_missing_core for the identical
    pagination shape this mirrors."""
    session = session if session is not None else get_requests_session()

    items = []
    offset = 0
    while True:
        resp = session.get(
            f"{admin_api_url}/products",
            params={"missing_coverstock": "true", "limit": page_limit, "offset": offset},
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
    """Tolerates per-product errors -- see backfill_core_ids.run's
    docstring for the reasoning, identical here."""
    list_products = list_fn if list_fn is not None else list_products_missing_coverstock
    rescrape = rescrape_fn if rescrape_fn is not None else rescrape_product

    products = list_products(admin_api_url, token)
    logger.info("Found %d product(s) missing a coverstock_id", len(products))

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
