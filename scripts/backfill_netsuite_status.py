#!/usr/bin/env python3
"""
One-off correction for the MOTIV/netsuite status bug -- see
src/netsuite_product_scraper/app.py's module docstring "REAL INCIDENT"
section and src/admin_api/service.backfill_netsuite_status's docstring
for the full root-cause writeup.

WHY THIS EXISTS: netsuite_product_scraper._process_one used to default a
missing job["status"] to "current" rather than falling back to
discovered_urls.status_path, and admin_api's queue_rescrape (the generic
"Rescrape" button, and scripts/backfill_core_ids.py's catalog-wide core
backfill) publishes jobs with no status key at all. Combined with
upsert_product's non-coalescing `status = excluded.status` overwrite,
every status-less rescrape permanently clobbered a real 'retired' MOTIV
product back to 'current'. Confirmed live: discovered_urls correctly held
60 current / 374 retired MOTIV URLs, but every one of the 202 products
actually scraped into `products` showed status='current'.

The actual code fix (get_status_for_url, mirroring shopify_product_
scraper's function of the same name) stops this from happening on future
scrapes, but does nothing for the 202 rows already wrong -- those won't
self-correct until each one happens to get rescraped again. This script
is the one-off catch-up: a single bulk POST to the new admin_api endpoint
that corrects every already-wrong row directly from discovered_urls in
one pass, same "thin one-shot POST, not a list-then-iterate batch runner"
shape as backfill_last_video_discovery_at.py (see that script's own
docstring for the fuller reasoning on why this class of fix doesn't need
pagination).

Idempotent and safe to re-run: the server-side function only touches rows
that still disagree with discovered_urls, so a second run just reports
products_corrected: 0.

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    python3 scripts/backfill_netsuite_status.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_netsuite_status")

# Same "probably transient" retry bucket every other admin_api-calling
# script in this project uses (see backfill_core_ids.py's
# get_requests_session for the fuller Lambda-concurrency-throttle
# writeup). backoff_factor=1 with the default urllib3 formula waits
# 1s/2s/4s/8s/16s between the 5 attempts.
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)
RETRY_TOTAL = 5
RETRY_BACKOFF_FACTOR = 1


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


def backfill(admin_api_url: str, token: str, session=None) -> dict:
    """Calls POST /admin/backfill-netsuite-status once. session defaults
    to a fresh retry-enabled one (see get_requests_session) but is
    overridable so tests can inject a fake transport instead of hitting
    the network."""
    session = session if session is not None else get_requests_session()

    resp = session.post(
        f"{admin_api_url}/admin/backfill-netsuite-status",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def run(admin_api_url: str, token: str, backfill_fn=None) -> dict:
    backfill_call = backfill_fn if backfill_fn is not None else backfill
    result = backfill_call(admin_api_url, token)
    logger.info(
        "Corrected %d MOTIV product(s) whose status had been clobbered back to 'current'",
        result.get("products_corrected", 0),
    )
    return result


def main():
    admin_api_url = os.environ.get("ADMIN_API_URL")
    token = os.environ.get("ADMIN_API_TOKEN")
    if not admin_api_url or not token:
        logger.error("ADMIN_API_URL and ADMIN_API_TOKEN must both be set -- see this script's module docstring for setup.")
        sys.exit(1)

    result = run(admin_api_url, token)
    logger.info("Done: %s", result)


if __name__ == "__main__":
    main()
