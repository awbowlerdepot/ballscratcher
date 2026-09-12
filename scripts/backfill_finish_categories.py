#!/usr/bin/env python3
"""
One-off (and re-runnable) correction populating products.finish_category
(db/migrations/035_products_finish_category.sql) from the raw products.
factory_finish text every scraper already captures.

WHY THIS EXISTS: Al asked directly, after the 2026-09-11 fix telling
Gemini not to alter a ball's surface finish in generated article images,
"are we capturing the finish for these balls?" factory_finish itself was
already captured by every scraper, but only as a raw manufacturer-specific
string ("500/1000/2000 Siaair Micro Pad", "5000 Grit LSS") -- never
normalized into anything resembling a matte/satin/glossy sheen category.
Migration 035 added the column; src/admin_api/service.py's
classify_factory_finish is the actual classification logic (dull/satin/
polished, or None if unclassifiable) -- see that function's own docstring
and migration 035's header comment for the full bucket-definition research
writeup, including Al's explicit caution that different manufacturers'
pads/polishes "could be mapped as similar but they are not 100% the same."

Same shape as scripts/backfill_last_video_discovery_at.py: a single bulk
server-side correction (see service.backfill_finish_categories's
docstring), so this script is just a thin one-shot POST, not a list-then-
iterate batch runner. Safe to re-run at any time, INCLUDING after
classify_factory_finish's own logic changes -- unlike backfill_last_video_
discovery_at's NULL-only guard, the server-side function intentionally
re-classifies and overwrites every row with factory_finish set, since
finish_category is a fully-derived value with no independent source of
truth to protect.

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    python3 scripts/backfill_finish_categories.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_finish_categories")

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
    """Calls POST /admin/backfill-finish-categories once. session defaults
    to a fresh retry-enabled one (see get_requests_session) but is
    overridable so tests can inject a fake transport instead of hitting
    the network."""
    session = session if session is not None else get_requests_session()

    resp = session.post(
        f"{admin_api_url}/admin/backfill-finish-categories",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def run(admin_api_url: str, token: str, backfill_fn=None) -> dict:
    backfill_call = backfill_fn if backfill_fn is not None else backfill
    result = backfill_call(admin_api_url, token)
    logger.info(
        "Checked %d product(s) with a factory_finish value, updated %d whose computed "
        "finish_category changed",
        result.get("products_with_factory_finish", 0),
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
