#!/usr/bin/env python3
"""
Backfill helper for the cores table (migration 007 -- see
src/admin_api/service.py's queue_rescrape/resolve_scrape_queue_env_var and
each *_product_scraper's get_or_create_core_id).

WHY THIS EXISTS: core_id only gets set on a product the next time it's
actually scraped -- migration 007 doesn't (and can't) backfill it for
already-scraped products, since core name only exists on the manufacturer's
own page, not anywhere already in this database. Nothing else re-triggers a
scrape for an already-known product on its own (the next natural sitemap
diff only fires for a genuinely new/changed URL, not one whose lastmod is
unchanged). This script finds every product currently missing a core_id via
GET /products?missing_core=true and calls POST /products/{id}/rescrape for
each one, which republishes that product's {url, brand_id} onto whichever
platform's scrape queue it belongs to (see queue_rescrape's
SCRAPE_QUEUE_ENV_VAR_BY_PLATFORM) -- same shape every one of the four
product scrapers already accepts for a direct/manual invocation, so no new
scraper-side code was needed for this.

This is a slower backfill than a plain DB update would be: it re-fetches
and re-parses the entire product page (not just the core field), which is
correct-by-construction (product_scraper's upsert_product coalesces on
conflict, so nothing else gets clobbered) but means this is bounded by
however fast each scraper's own SQS-triggered Lambda works through its
queue, not by this script's own pace -- this script only enqueues, it
doesn't wait for the actual scrape to finish. Re-run it later (or just
check GET /products?missing_core=true's count) to see how much is left.

A product on a platform with no scraper deployed yet (shopify --
Hammer/Track/Ebonite, see DEPLOY_RUNBOOK.md) will show up in the
missing_core list forever, since there's nothing to actually rescrape it
with -- queue_rescrape returns {"queued": False, "reason": "no scraper
deployed..."} for these rather than erroring, and this script logs that
and moves on rather than treating it as a failure.

TRANSIENT 503s FROM ADMIN_API_URL (real, confirmed this session): running
this against the whole catalog (not just one brand) triggers every
platform's scraper Lambda at once as their queues fill up from this
script's own rescrape calls -- five separate SQS-triggered functions, all
sharing the same account's Lambda concurrency pool as AdminApiFunction
itself. Confirmed via CloudWatch (`Throttles` metric nonzero on
bowling-scraper-admin-api during a live run, `aws lambda
get-account-settings` showing UnreservedConcurrentExecutions=10 -- AWS's
default low tier for a new/unverified account, not the usual 1000):
AdminApiFunction's own invocation gets throttled by the Lambda service
before it ever starts, which HTTP API v2 (AdminHttpApi) surfaces as a
bare 503 with nothing in AdminApiFunction's own CloudWatch log group,
since the function code never actually ran. get_requests_session() below
retries on exactly this (and the neighboring 429/500/502/504 cases) with
exponential backoff, so a transient throttle self-heals within the same
run instead of needing "will retry next run" to actually mean re-running
the whole script by hand. template.yaml now also reserves a small,
guaranteed concurrency slot for AdminApiFunction specifically (see that
resource's own comment) so it's never fully starved -- the two fixes are
complementary: reserved concurrency shrinks how often this happens,
retries absorb whatever still gets through. The real structural fix is
requesting a Lambda concurrency quota increase for this account via AWS
Service Quotas -- 10 total concurrent executions across an account
running five-plus Lambda functions is going to keep being tight even
with both mitigations in place.

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    python3 scripts/backfill_core_ids.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_core_ids")

DEFAULT_PAGE_LIMIT = 200

# Retried status codes: 429 (explicit rate limit) and 500/502/503/504 (503
# is what a Lambda-concurrency throttle actually surfaces as through HTTP
# API v2 -- see module docstring; the other three are included as the
# same "probably transient, safe to retry" bucket every other integration
# failure in this family falls into). backoff_factor=1 with the default
# urllib3 formula (backoff_factor * 2^(retry_number - 1)) waits
# 1s/2s/4s/8s/16s between the 5 attempts -- long enough for a Lambda
# throttle spike from the scraper swarm to clear on its own, short enough
# not to stall a large backfill run per product.
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)
RETRY_TOTAL = 5
RETRY_BACKOFF_FACTOR = 1


def get_requests_session():
    """Builds a requests.Session with urllib3 Retry mounted on both
    http/https -- shared by list_products_missing_core and
    rescrape_product below so a transient throttle/5xx anywhere in this
    script's admin_api traffic gets retried the same way. A fresh session
    per call (rather than one module-level singleton) keeps this easy to
    monkeypatch/replace in tests without worrying about cross-test state."""
    import requests
    from urllib3.util.retry import Retry

    session = requests.Session()
    retry = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUS_FORCELIST,
        allowed_methods=("GET", "POST"),
        raise_on_status=False,  # let raise_for_status() below report the final failure, not urllib3's own exception shape
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def list_products_missing_core(admin_api_url: str, token: str, page_limit: int = DEFAULT_PAGE_LIMIT,
                                session=None) -> list:
    """Paginates GET /products?missing_core=true -- same pagination shape
    as every other script in this project (backfill_video_review_rollups.
    list_products_needing_refresh, home_transcript_fetcher.
    list_candidates_needing_transcripts, auto_approve_video_candidates.
    list_pending_candidates). session defaults to a fresh retry-enabled
    one (see get_requests_session) but is overridable so tests can inject
    a fake transport instead of hitting the network."""
    session = session if session is not None else get_requests_session()

    items = []
    offset = 0
    while True:
        resp = session.get(
            f"{admin_api_url}/products",
            params={"missing_core": "true", "limit": page_limit, "offset": offset},
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
    other batch script in this project. A {"queued": False, ...} result
    (unsupported platform, misconfigured queue) is logged and counted
    separately from a real error -- it's an expected outcome, not a
    failure."""
    list_products = list_fn if list_fn is not None else list_products_missing_core
    rescrape = rescrape_fn if rescrape_fn is not None else rescrape_product

    products = list_products(admin_api_url, token)
    logger.info("Found %d product(s) missing a core_id", len(products))

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
