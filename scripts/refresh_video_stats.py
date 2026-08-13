#!/usr/bin/env python3
"""
Thin trigger for video_discovery's {"refresh_stats": true} job (migration
013/src/video_discovery/app.py's fetch_video_statistics/refresh_video_
stats) -- re-pulls current view/like/comment counts (and duration/
description) for existing product_videos rows.

WHY THIS EXISTS: video_discovery now enriches brand-new candidates with
stats automatically at discovery time, but view counts are a snapshot, not
a fixed fact -- they go stale the moment they're written, and a candidate
discovered before this feature shipped has no stats at all yet. This
script is how existing rows (not just future ones) get covered, and how
"current" stays actually current if you re-run it periodically (e.g. a
weekly cron). Al: "for the videos can we get pull down more data points
from the videos, date it was added current view counts and any other data
that make sense."

Unlike most scripts in this directory, this isn't a per-item loop over a
paginated list -- it's a single POST to /admin/refresh-video-stats, which
invokes VideoDiscoveryFunction directly (async) to do the actual selecting/
fetching/updating server-side. Same shape as backfill_last_video_discovery_
at.py for that same reason (see its own module docstring). This means the
POST returns immediately with {"queued": True, ...}, not a result summary
-- check CloudWatch logs for bowling-scraper-video-discovery to see what
actually happened (candidates_checked/candidates_updated), same as any
other manual VideoDiscoveryFunction invocation.

LIMIT (optional): caps how many product_videos rows get refreshed in this
one invocation -- omitted, VideoDiscoveryFunction falls back to its own
DEFAULT_REFRESH_STATS_LIMIT (200). Re-run this script (or just wait for a
scheduled re-run) to work through more of the table; repeated runs
naturally prioritize whatever's most overdue (see select_video_ids_
needing_stats_refresh's stats_fetched_at asc nulls first ordering), so
there's no bookkeeping needed between runs the way a real pagination loop
would need.

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    export LIMIT="200"  # optional
    python3 scripts/refresh_video_stats.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("refresh_video_stats")

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


def trigger_refresh(admin_api_url: str, token: str, limit: int = None, session=None) -> dict:
    """Calls POST /admin/refresh-video-stats once, with an optional
    ?limit=. session defaults to a fresh retry-enabled one (see
    get_requests_session) but is overridable so tests can inject a fake
    transport instead of hitting the network."""
    session = session if session is not None else get_requests_session()

    params = {"limit": limit} if limit is not None else None
    resp = session.post(
        f"{admin_api_url}/admin/refresh-video-stats",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def run(admin_api_url: str, token: str, limit: int = None, trigger_fn=None) -> dict:
    trigger = trigger_fn if trigger_fn is not None else trigger_refresh
    result = trigger(admin_api_url, token, limit=limit)
    logger.info("Triggered: %s", result)
    return result


def main():
    admin_api_url = os.environ.get("ADMIN_API_URL")
    token = os.environ.get("ADMIN_API_TOKEN")
    if not admin_api_url or not token:
        logger.error("ADMIN_API_URL and ADMIN_API_TOKEN must both be set -- see this script's module docstring for setup.")
        sys.exit(1)
    limit_str = os.environ.get("LIMIT")
    limit = int(limit_str) if limit_str else None

    result = run(admin_api_url, token, limit=limit)
    if not result.get("queued"):
        logger.error("Not queued: %s", result.get("reason"))
        sys.exit(1)


if __name__ == "__main__":
    main()
