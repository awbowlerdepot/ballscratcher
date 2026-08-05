#!/usr/bin/env python3
"""
One-off correction for migration 005 (db/migrations/005_products_last_
video_discovery_at.sql -- see that file's own comment) not backfilling
existing data.

WHY THIS EXISTS: migration 005 added products.last_video_discovery_at
with no backfill, so any product searched before that migration ran
(under the old, buggy `updated_at desc` rotation -- see src/video_
discovery/app.py's module docstring, ROTATION section) has real
product_videos rows but a NULL last_video_discovery_at. Since rotation
now sorts NULLs first, those already-searched products keep jumping the
queue ahead of products that have genuinely never been searched. Caught
via a live count mismatch: a query against this catalog found 231
products with a NULL last_video_discovery_at but only 174 with zero
product_videos rows at all -- a ~57-product gap of products that were
really covered, just not marked as such under the new column.

Unlike every other script in this scripts/ directory, this isn't a
per-product loop over a paginated list -- it's a single bulk server-side
correction (see service.backfill_last_video_discovery_at's docstring),
so this script is just a thin one-shot POST, not a list-then-iterate
batch runner. Idempotent and safe to re-run: the server-side function
only ever sets a currently-NULL column, so running this twice in a row
(or after a fresh video_discovery invocation has legitimately searched
some of the same products) just means the second run updates fewer rows,
never wrong ones.

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    python3 scripts/backfill_last_video_discovery_at.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_last_video_discovery_at")

# Retried status codes: same "probably transient" bucket every other
# admin_api-calling script in this project uses (see backfill_core_ids.py's
# get_requests_session for the fuller Lambda-concurrency-throttle writeup
# behind why 503 is in this list specifically). backoff_factor=1 with the
# default urllib3 formula waits 1s/2s/4s/8s/16s between the 5 attempts.
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
    """Calls POST /admin/backfill-last-video-discovery-at once. session
    defaults to a fresh retry-enabled one (see get_requests_session) but
    is overridable so tests can inject a fake transport instead of
    hitting the network."""
    session = session if session is not None else get_requests_session()

    resp = session.post(
        f"{admin_api_url}/admin/backfill-last-video-discovery-at",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def run(admin_api_url: str, token: str, backfill_fn=None) -> dict:
    backfill_call = backfill_fn if backfill_fn is not None else backfill
    result = backfill_call(admin_api_url, token)
    logger.info(
        "Checked %d product(s) with video search history, updated %d that were missing "
        "last_video_discovery_at",
        result.get("products_with_video_history", 0),
        result.get("products_updated", 0),
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
