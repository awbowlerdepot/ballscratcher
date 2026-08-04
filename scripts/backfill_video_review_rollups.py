#!/usr/bin/env python3
"""
Backfill/refresh helper for the "summary of summaries" feature
(products.video_reviews_summary -- see src/video_summarizer/app.py's
module docstring, SUMMARY OF SUMMARIES section, and
src/admin_api/service.py's refresh_video_reviews_rollup).

WHY THIS EXISTS: video_summarizer regenerates the rollup automatically,
but only as a side effect of a video getting a NEW summary written. Any
product whose videos were already approved+summarized before that
automatic trigger (or before this endpoint) existed has no rollup and
nothing will build one on its own -- the browser transcript cron won't
revisit an already-summarized video, and nothing else re-triggers
video_summarizer for it. This script finds every product currently
missing (or stale on) its rollup via GET /products?needs_video_summary_
refresh=true (see list_products' docstring for exactly what that filter
means) and calls POST /products/{id}/refresh-video-summary for each.

Also useful beyond a one-time backfill: re-running this after a bulk
reassign/delete cleanup (DEPLOY_RUNBOOK.md 6i, step 5) picks up any
product whose set of approved+summarized videos changed as a result,
since that filter recomputes off the real current count each time -- not
just "run once and never again."

REFRESH_ALL mode: set the REFRESH_ALL environment variable (any of
"1"/"true"/"yes", case-insensitive) to switch from the default
needs_video_summary_refresh filter to the broader has_approved_video_
summaries filter -- every product with at least one approved+summarized
video, not just the ones whose stored video count looks stale. Built for
the products.description backfill (DEPLOY_RUNBOOK.md 6i, step 9): scraping
a description onto an already-summarized product doesn't change its
approved+summarized video count, so needs_video_summary_refresh's
staleness check has no way to notice that rollup could now say more with
the new context -- see list_products'/has_approved_video_summaries'
docstring in src/admin_api/service.py for the full reasoning. This mode
is meant for an occasional deliberate "regenerate the whole catalog" pass
(real Bedrock cost across every eligible product, not just the stale
ones), not routine/scheduled use -- leave REFRESH_ALL unset for that.

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    python3 scripts/backfill_video_review_rollups.py

    # Or, for a one-time catalog-wide regeneration:
    REFRESH_ALL=true python3 scripts/backfill_video_review_rollups.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_video_review_rollups")

DEFAULT_PAGE_LIMIT = 200


def list_products_needing_refresh(admin_api_url: str, token: str, page_limit: int = DEFAULT_PAGE_LIMIT,
                                   refresh_all: bool = False) -> list:
    """Paginates GET /products?needs_video_summary_refresh=true (or, with
    refresh_all=True, ?has_approved_video_summaries=true -- see module
    docstring's REFRESH_ALL section) -- same pagination shape as every
    other script in this project (home_transcript_fetcher.
    list_candidates_needing_transcripts, auto_approve_video_candidates.
    list_pending_candidates)."""
    import requests

    filter_param = "has_approved_video_summaries" if refresh_all else "needs_video_summary_refresh"

    items = []
    offset = 0
    while True:
        resp = requests.get(
            f"{admin_api_url}/products",
            params={filter_param: "true", "limit": page_limit, "offset": offset},
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


def refresh_product(admin_api_url: str, token: str, product_id: str) -> dict:
    import requests

    resp = requests.post(
        f"{admin_api_url}/products/{product_id}/refresh-video-summary",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def run(admin_api_url: str, token: str, list_fn=None, refresh_fn=None, refresh_all: bool = False) -> dict:
    """Tolerates per-product refresh errors -- one Bedrock hiccup or a
    genuine race (a product's videos changed again between listing and
    refreshing) shouldn't stop the rest of the batch, same principle as
    every other batch script in this project.

    refresh_all: see module docstring's REFRESH_ALL section -- only
    affects the default list_fn (list_products_needing_refresh's
    filter_param), ignored if a caller supplies its own list_fn (e.g.
    tests), matching how limit/pagination are already caller-controlled
    there."""
    if list_fn is None:
        def list_fn(url, tok):
            return list_products_needing_refresh(url, tok, refresh_all=refresh_all)
    refresh = refresh_fn if refresh_fn is not None else refresh_product

    products = list_fn(admin_api_url, token)
    logger.info("Found %d product(s) needing a video-review rollup refresh", len(products))

    refreshed = 0
    errors = 0
    for product in products:
        product_id = product["id"]
        try:
            result = refresh(admin_api_url, token, product_id)
            if result.get("rollup_regenerated"):
                refreshed += 1
                logger.info("Refreshed product_id=%s name=%r video_count=%s",
                            product_id, product.get("name"), result.get("video_count"))
            else:
                logger.info("product_id=%s name=%r: %s", product_id, product.get("name"), result.get("reason"))
        except Exception:
            errors += 1
            logger.exception("Failed to refresh product_id=%s name=%r -- will retry next run",
                              product_id, product.get("name"))

    return {"total": len(products), "refreshed": refreshed, "errors": errors}


def main():
    admin_api_url = os.environ.get("ADMIN_API_URL")
    token = os.environ.get("ADMIN_API_TOKEN")
    if not admin_api_url or not token:
        logger.error("ADMIN_API_URL and ADMIN_API_TOKEN must both be set -- see this script's module docstring for setup.")
        sys.exit(1)

    refresh_all = os.environ.get("REFRESH_ALL", "").strip().lower() in ("1", "true", "yes")
    if refresh_all:
        logger.info("REFRESH_ALL is set -- regenerating every product's rollup, not just stale ones.")

    summary = run(admin_api_url, token, refresh_all=refresh_all)
    logger.info("Done: %s", summary)

    if summary["total"] > 0 and summary["refreshed"] == 0 and summary["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
