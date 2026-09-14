#!/usr/bin/env python3
"""
One-off (and safely re-runnable) backfill populating product_articles.slug
(db/migrations/036_product_articles_slug.sql) for already-approved
articles that predate that migration.

WHY THIS EXISTS: Al asked "can we make the slugs for the pages more
human readable, does google still prefer that?" -- confirmed via
Google's own developer documentation, still yes. Migration 036 added
the column; admin_api/service.py's approve_article now computes and
persists a slug on every article's FIRST-EVER approval going forward
(see that function's own docstring), but that only covers NEW
approvals -- every article that was already 'approved' before the
migration landed still has slug = null the moment the column is added.
This script closes that gap in one bulk pass.

Same shape as scripts/backfill_finish_categories.py: a single bulk
server-side correction (see service.backfill_article_slugs's
docstring), so this is just a thin one-shot POST, not a list-then-
iterate batch runner. Safe to re-run at any time -- unlike
backfill_finish_categories (which intentionally re-classifies every
row), this one only ever fills in a currently-null slug and never
touches an article that already has one, since a slug must stay
stable for the life of an article once it's ever been public (see
036_product_articles_slug.sql's header comment on why a changing slug
would orphan external links/the 301-redirect mapping).

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    python3 scripts/backfill_article_slugs.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_article_slugs")

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
    """Calls POST /admin/backfill-article-slugs once. session defaults
    to a fresh retry-enabled one (see get_requests_session) but is
    overridable so tests can inject a fake transport instead of hitting
    the network."""
    session = session if session is not None else get_requests_session()

    resp = session.post(
        f"{admin_api_url}/admin/backfill-article-slugs",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def run(admin_api_url: str, token: str, backfill_fn=None) -> dict:
    backfill_call = backfill_fn if backfill_fn is not None else backfill
    result = backfill_call(admin_api_url, token)
    logger.info(
        "Checked %d approved article(s) missing a slug, backfilled %d",
        result.get("approved_articles_missing_slug", 0),
        result.get("articles_updated", 0),
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
    logger.info(
        "Next: redeploy bowlerdepot-learn (npm run build) so prerender.ts "
        "picks up the new slugs, and re-run scripts/sync-slug-redirects.mjs "
        "(or just push to main -- the GitHub Actions workflow runs it on "
        "every deploy) so the CloudFront KeyValueStore has an old-uuid -> "
        "new-slug entry for every one of these."
    )


if __name__ == "__main__":
    main()
