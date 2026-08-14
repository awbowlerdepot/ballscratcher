#!/usr/bin/env python3
"""
One-off cleanup for a real duplicate-row bug found live in production.

WHY THIS EXISTS: Al, after filling in a previously-blank price_sites.
base_url and re-running discovery: "there are duplicates now, the ones
before having the baseurl and now the ones that have it... same record
just has different link."

Root cause: extract_bigcommerce_price_fields (src/price_checker/app.py)
falls back to the raw relative custom_url when a price_sites row's
base_url isn't configured, so a product_price_sources row discovered
before base_url was filled in got a relative product_url stored.
insert_price_source_candidates' ON CONFLICT DO NOTHING is keyed on the
literal (product_id, price_site_id, product_url) triple (014_price_
tracking.sql) -- once base_url got filled in, the next discovery run
computed a different (absolute) product_url for the exact same real-world
product+site pair, so the conflict target didn't match and a second row
got INSERTed instead of the first one being corrected in place.

price_checker.upsert_bigcommerce_price_source_candidate is the matching
root-cause fix (see that function's own docstring) that stops this from
recurring going forward -- this script only cleans up rows that already
duplicated before that fix shipped.

Unlike every per-product/paginated script in this scripts/ directory,
this is a single bulk server-side correction (see service.dedupe_
product_price_sources' docstring for the actual merge logic: keep the
approved+active row as survivor, migrate any product_price_history/
product_sku_stock_history rows onto it, delete the rest, and correct the
survivor's product_url to whichever variant in the group is actually
resolved), so this script is just a thin one-shot POST, same shape as
backfill_last_video_discovery_at.py. Idempotent and safe to re-run: a
catalog with no duplicate groups left just returns groups_merged=0
rows_deleted=0.

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    python3 scripts/dedupe_product_price_sources.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("dedupe_product_price_sources")

# Same "probably transient" retry bucket every other admin_api-calling
# script in this project uses (see backfill_core_ids.py's get_requests_
# session for the fuller Lambda-concurrency-throttle writeup behind why
# 503 is in this list specifically). backoff_factor=1 with the default
# urllib3 formula waits 1s/2s/4s/8s/16s between the 5 attempts.
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


def dedupe(admin_api_url: str, token: str, session=None) -> dict:
    """Calls POST /admin/dedupe-price-sources once. session defaults to a
    fresh retry-enabled one (see get_requests_session) but is overridable
    so tests can inject a fake transport instead of hitting the network."""
    session = session if session is not None else get_requests_session()

    resp = session.post(
        f"{admin_api_url}/admin/dedupe-price-sources",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def run(admin_api_url: str, token: str, dedupe_fn=None) -> dict:
    dedupe_call = dedupe_fn if dedupe_fn is not None else dedupe
    result = dedupe_call(admin_api_url, token)
    logger.info(
        "Merged %d duplicate group(s), deleted %d redundant row(s)",
        result.get("groups_merged", 0),
        result.get("rows_deleted", 0),
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
