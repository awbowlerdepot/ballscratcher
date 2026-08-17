#!/usr/bin/env python3
"""
Scripted equivalent of clicking "Find price sources" on the admin site,
run catalog-wide instead of one product at a time -- Al: "can we script
clicking find price sources for all items that are current."

Every product detail page's "Find price sources" button calls POST
/products/{id}/discover-price-sources for exactly that one product (see
service.queue_price_discovery's docstring). This script instead hits POST
/admin/discover-all-price-sources, the catalog-wide sibling of that same
button (see service.queue_price_discovery_batch's docstring and app.py's
own "same shape as /admin/refresh-video-stats" comment on that route) --
NO product_ids/brand_id needed, since price_checker's own discovery job
already defaults to every product.status = 'current' row when neither is
given (see price_checker.fetch_products_to_discover). "All items that are
current" is this endpoint's own default scope, not something this script
needs to compute itself.

Unlike most scripts in this directory, this isn't a per-item loop over a
paginated list -- it's a POST to a bulk server-side trigger, which invokes
PriceCheckerFunction directly (async, "Event" invocation) to do the actual
selecting/searching/inserting server-side. Same shape as refresh_video_
stats.py for that same reason (see its own module docstring). This means
each POST returns immediately with {"queued": True, ...}, not a result
summary -- check CloudWatch logs for bowling-scraper-price-checker to see
what actually happened (products_searched/sites_searched/new_candidates),
same as any other manual PriceCheckerFunction invocation.

LIMIT (optional): caps how many products get searched in ONE invocation --
omitted, PriceCheckerFunction falls back to its own DEFAULT_MAX_PRODUCTS_
PER_DISCOVERY_INVOCATION (100). A catalog with more than LIMIT current
products needs more than one invocation to search all of them.

SCRAPE_ONLY (optional, default "false"): skips every 'api' fetch_method
site (BowlerDepot) for this run -- Al, re-running this catalog-wide while
testing a scrape-site config fix: "can we not run the bowlerdepot price
sources in this one, they have inventory numbers too." BowlerDepot is
also where product_sku_stock_history's per-SKU inventory counts come
from (017), and it's already kept fresh by bowlerdepot_reconciliation's
own daily schedule independent of this script -- set SCRAPE_ONLY=true
to leave it alone and only (re-)search the generic scrape sites.

REPEAT / INTERVAL_SECONDS (both optional, default 1 / 300): rather than
making Al re-run this script by hand to work through the rest of a large
catalog, REPEAT fires the same trigger that many times in one run, sleeping
INTERVAL_SECONDS between each. Each invocation processes its own products
(marking price_checker.mark_product_price_discovery_searched on the way,
even for a product where every configured site search failed -- see that
module's own "one bad row can't stop the batch" convention) BEFORE it
returns, then naturally rotates the next call onto whatever's most overdue
next (fetch_products_to_discover's own "p.last_price_discovery_at asc nulls
first" ordering, same idea as refresh_video_stats.py's stats_fetched_at
ordering) -- so there's no bookkeeping needed between calls the way a real
pagination loop would need, same as that script's own docstring explains.

INTERVAL_SECONDS defaults to 300 specifically because the invocation is
async ("Event") -- this script's POST returns before PriceCheckerFunction
has actually finished searching that batch, so firing the next call too
soon risks it re-selecting the SAME still-unmarked products instead of
advancing. PriceCheckerFunction's own Lambda timeout is 280s (template.
yaml's own comment: "network-bound against arbitrary, unpredictably slow
third-party sites"), so 300s gives one invocation room to either finish or
hit its own timeout before the next one starts.

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    export LIMIT="100"            # optional, per-invocation cap
    export REPEAT="5"             # optional, default 1 -- one click's worth
    export INTERVAL_SECONDS="300" # optional, only matters if REPEAT > 1
    export SCRAPE_ONLY="true"     # optional, default false -- skip BowlerDepot
    python3 scripts/discover_all_price_sources.py
"""
import logging
import os
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("discover_all_price_sources")

# Retried status codes: same "probably transient" bucket every other
# admin_api-calling script in this project uses (see backfill_core_ids.py's
# get_requests_session for the fuller Lambda-concurrency-throttle writeup
# behind why 503 is in this list specifically). backoff_factor=1 with the
# default urllib3 formula waits 1s/2s/4s/8s/16s between the 5 attempts.
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)
RETRY_TOTAL = 5
RETRY_BACKOFF_FACTOR = 1

DEFAULT_INTERVAL_SECONDS = 300


def get_requests_session():
    """Builds a requests.Session with urllib3 Retry mounted on both
    http/https -- same shape as every other script in this project. A
    fresh session per call (rather than a module-level singleton) keeps
    this easy to monkeypatch/replace in tests."""
    import requests
    from urllib3.util.retry import Retry

    session = requests.Session()
    retry = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUS_FORCELIST,
        allowed_methods=("POST",),
        raise_on_status=False,
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def trigger_discovery(admin_api_url: str, token: str, limit: int = None, scrape_only: bool = False, session=None) -> dict:
    """Calls POST /admin/discover-all-price-sources once, with an optional
    ?limit= and ?scrape_only=. session defaults to a fresh retry-enabled
    one (see get_requests_session) but is overridable so tests can inject
    a fake transport instead of hitting the network. scrape_only=True
    skips BowlerDepot ('api' fetch_method) for this run -- see this
    module's own docstring."""
    session = session if session is not None else get_requests_session()

    params = {}
    if limit is not None:
        params["limit"] = limit
    if scrape_only:
        params["scrape_only"] = "true"
    resp = session.post(
        f"{admin_api_url}/admin/discover-all-price-sources",
        params=params or None,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def run(admin_api_url: str, token: str, limit: int = None, repeat: int = 1,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS, scrape_only: bool = False,
        trigger_fn=None, sleep_fn=None) -> list:
    """Fires trigger_discovery up to `repeat` times, sleeping
    interval_seconds between calls (never after the last one). Stops
    early -- without sleeping first -- the moment a call comes back
    queued=False (e.g. PRICE_CHECKER_FUNCTION_NAME isn't configured on
    this deployment); retrying the exact same misconfiguration on a timer
    wouldn't help. Returns the list of every result dict actually
    received, in order, so a caller (or a test) can see how many of the
    requested repeats actually ran."""
    trigger = trigger_fn if trigger_fn is not None else trigger_discovery
    sleep = sleep_fn if sleep_fn is not None else time.sleep

    results = []
    for i in range(repeat):
        result = trigger(admin_api_url, token, limit=limit, scrape_only=scrape_only)
        logger.info("Triggered (%d/%d): %s", i + 1, repeat, result)
        results.append(result)
        if not result.get("queued"):
            logger.error("Not queued: %s -- stopping early.", result.get("reason"))
            break
        if i < repeat - 1:
            logger.info("Sleeping %ds before the next batch...", interval_seconds)
            sleep(interval_seconds)
    return results


def main():
    admin_api_url = os.environ.get("ADMIN_API_URL")
    token = os.environ.get("ADMIN_API_TOKEN")
    if not admin_api_url or not token:
        logger.error("ADMIN_API_URL and ADMIN_API_TOKEN must both be set -- see this script's module docstring for setup.")
        sys.exit(1)
    limit_str = os.environ.get("LIMIT")
    limit = int(limit_str) if limit_str else None
    repeat = int(os.environ.get("REPEAT", "1"))
    interval_seconds = int(os.environ.get("INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS)))
    scrape_only = os.environ.get("SCRAPE_ONLY", "false").strip().lower() in ("1", "true", "yes")

    results = run(
        admin_api_url, token, limit=limit, repeat=repeat,
        interval_seconds=interval_seconds, scrape_only=scrape_only,
    )
    if not results or not results[-1].get("queued"):
        sys.exit(1)


if __name__ == "__main__":
    main()
