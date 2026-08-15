#!/usr/bin/env python3
"""
One-off correction for every product whose plotter position was written
under estimate_oil_motion's ORIGINAL, badly-miscalibrated constants.

WHY THIS EXISTS: Al: "i feel like it is way off for most balls" -- backed
up by DEPLOY_RUNBOOK.md 6m's 2026-08-12 spot-check against the 32 real
Brunswick-chart products (only 2/32 exact oil matches, mean error 3.3 on
the 1-16 scale). Once estimate_oil_motion's constants were refit against
that real data (see src/public_api/service.py's own header comment on
that function for the refit and its measured accuracy), every product
scraped from then on automatically gets the better estimate -- but
nothing revisits a product that was ALREADY estimated under the old
formula; oil_rating/motion_rating are written once and never
recomputed automatically (see list_plotter_positions' own docstring).
This script is the one-time "go fix everything the old formula already
got wrong" pass.

Unlike every rescrape-queue script in this scripts/ directory, this isn't
a per-product loop over a paginated list -- it's a single bulk
server-side correction (see service.reestimate_plotter_positions'
docstring), so this is just a thin one-shot POST, same shape as
backfill_last_video_discovery_at.py. Idempotent and safe to re-run: it
only ever touches rows still marked oil_motion_source='estimated' at
write time, never 'chart' or 'manual', so running it twice just means
the second run updates zero rows (nothing left to fix) rather than doing
anything wrong.

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    python3 scripts/reestimate_plotter_positions.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reestimate_plotter_positions")

# Same "probably transient" retry bucket every other admin_api-calling
# script in this project uses -- see backfill_core_ids.py's get_requests_
# session for the fuller writeup.
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)
RETRY_TOTAL = 5
RETRY_BACKOFF_FACTOR = 1


def get_requests_session():
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


def reestimate(admin_api_url: str, token: str, session=None) -> dict:
    """Calls POST /admin/reestimate-plotter-positions once. session
    defaults to a fresh retry-enabled one but is overridable so tests can
    inject a fake transport instead of hitting the network."""
    session = session if session is not None else get_requests_session()

    resp = session.post(
        f"{admin_api_url}/admin/reestimate-plotter-positions",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def run(admin_api_url: str, token: str, reestimate_fn=None) -> dict:
    reestimate_call = reestimate_fn if reestimate_fn is not None else reestimate
    result = reestimate_call(admin_api_url, token)
    logger.info(
        "Checked %d product(s) marked 'estimated', recomputed %d with the current formula",
        result.get("products_estimated", 0),
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
