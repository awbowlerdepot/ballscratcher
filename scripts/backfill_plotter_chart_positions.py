#!/usr/bin/env python3
"""
One-time matching pass that writes the AUTHORITATIVE oil/motion chart
positions (migration 011, products.oil_rating/motion_rating) onto real
catalog products -- see src/public_api/service.py's module comment above
estimate_oil_motion for the full backstory: Al shared an existing
interactive plotter he'd built in another Cowork project, hand-digitized
from Brunswick's own published "Ball Motion Comparison Chart" PDF (Form
#0526-19, Jul/Aug 2026). scripts/data/brunswick_chart_positions.json is
that plotter's own 56-entry dataset (brand/name/oil/motion), extracted
directly from its balls.js.

WHY THIS IS A SEPARATE, MANUAL-REVIEW-FRIENDLY SCRIPT rather than an
automatic match-and-write inside a migration or public_api itself: the
chart's own ball names are marketing copy ("Crown Victory Pearl"), not
guaranteed to equal this catalog's own product.name text exactly, and a
wrong auto-match would silently mislabel a real product with someone
else's chart position -- worse than leaving it to the algorithmic
estimate (public_api.estimate_oil_motion), which is at least honestly
flagged as an estimate. So this script is deliberately conservative:
it only writes a position for an EXACT (case-insensitive) brand + name
match. Zero matches or multiple candidates are logged for manual review
and never guessed at.

Matching approach: for each chart entry, resolve its brand name to a
brand_id via GET /brands (admin_api's version -- every brand, not just
ones with published products, since a chart entry might be for the
retired variant of a currently-unpublished-while-being-reviewed
product), then GET /products?brand_id=...&search=<name>&limit=200
(admin_api's ilike substring search casts a broad net), then filter the
results client-side for an exact case-insensitive product.name match.
Exactly one exact match -> confident, writes via PATCH /products/{id}/
plotter-position. Zero or more than one -> logged as needing manual
review, skipped.

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    python3 scripts/backfill_plotter_chart_positions.py
"""
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_plotter_chart_positions")

CHART_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "brunswick_chart_positions.json")

# Real gap found 2026-08-12 (Al: "some of the balls on the example plotter
# are missing their actual values"): a first run of this script left 24 of
# 56 chart entries unmatched. Manually cross-checked every one against the
# live GET /products/plotter data (not guessed) and found most of those 24
# are genuinely retired/unpublished right now -- correctly left as
# 'estimated', not a bug. But five are real, currently-published products
# that this script's strict exact-match rejected purely because the
# chart's own name text (transcribed from balls.js, itself hand-typed off
# Brunswick's PDF) doesn't byte-for-byte match this catalog's real
# product.name -- a punctuation or word-order difference, same ball
# either way, confirmed by brand + oil/motion context and (for the Raw
# Hammer/Fury cases) the live product page's own name field:
# - "Raw Hammer Red / White / Purple" -> catalog has the brand's own
#   dash convention, "Raw Hammer - Red / White / Purple".
# - "Fury Orange / Red" / "Fury Emerald / Black" -> same dash convention,
#   "Fury - Orange / Red" / "Fury - Emerald / Black".
# - "Vibe Deep Ocean" -> catalog's real name is word-order-swapped,
#   "Deep Ocean Vibe".
# - "Widow Tour V1" -> catalog's real name has the "Black" prefix every
#   other Black Widow-line product also carries, "Black Widow Tour V1".
#
# Deliberately NOT included here (stayed 'estimated', on purpose): a
# handful of other near-misses were genuinely ambiguous rather than a
# clear rename -- e.g. chart's plain "Infinity Quest"/"Danger Zone"/"Guru
# Oracle"/"Zero Mercy" each have exactly one live product candidate whose
# name has an *extra* word ("... Pearl", "... Solid"), but there's no way
# to confirm from the data alone whether that's the same colorway the
# chart meant or a genuinely different one the chart doesn't cover -- per
# this script's whole design principle (never guess), those are left for
# a human to resolve by hand, not silently absorbed into this dict.
NAME_OVERRIDES = {
    ("Hammer", "Raw Hammer Red / White / Purple"): "Raw Hammer - Red / White / Purple",
    ("Brunswick", "Fury Orange / Red"): "Fury - Orange / Red",
    ("Brunswick", "Fury Emerald / Black"): "Fury - Emerald / Black",
    ("Hammer", "Vibe Deep Ocean"): "Deep Ocean Vibe",
    ("Hammer", "Widow Tour V1"): "Black Widow Tour V1",
}

# Same retry posture as every other backfill script in this project -- see
# backfill_core_ids.py's module docstring for the full story on the
# 503-from-Lambda-throttle issue this guards against.
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)
RETRY_TOTAL = 5
RETRY_BACKOFF_FACTOR = 1


def load_chart_entries(path: str = CHART_DATA_PATH) -> list:
    with open(path) as f:
        return json.load(f)


def get_requests_session():
    """Same shape as every other script in this project -- see
    backfill_core_ids.get_requests_session's docstring."""
    import requests
    from urllib3.util.retry import Retry

    session = requests.Session()
    retry = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUS_FORCELIST,
        allowed_methods=("GET", "PATCH"),
        raise_on_status=False,
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def list_brands(admin_api_url: str, token: str, session=None) -> list:
    session = session if session is not None else get_requests_session()
    resp = session.get(
        f"{admin_api_url}/brands",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("items", [])


def search_products(admin_api_url: str, token: str, brand_id: str, name: str, session=None) -> list:
    session = session if session is not None else get_requests_session()
    resp = session.get(
        f"{admin_api_url}/products",
        params={"brand_id": brand_id, "search": name, "limit": 200},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("items", [])


def set_plotter_position(admin_api_url: str, token: str, product_id: str, oil_rating: int,
                          motion_rating: int, session=None) -> dict:
    # source="chart" explicitly -- migration 012 added a default of
    # 'manual' to this endpoint (an admin correcting an estimate by hand
    # is its main real-world caller now), so this script has to say what
    # it actually is or every chart match would get mislabeled 'manual'.
    session = session if session is not None else get_requests_session()
    resp = session.patch(
        f"{admin_api_url}/products/{product_id}/plotter-position",
        json={"oil_rating": oil_rating, "motion_rating": motion_rating, "source": "chart"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def match_entry(entry: dict, brands_by_name_lower: dict, search_fn) -> dict:
    """Resolves one chart entry to at most one confident product match.
    Returns {"status": "matched", "product": {...}} / {"status":
    "no_brand"} / {"status": "no_match", "candidates": [...]} / {"status":
    "ambiguous", "candidates": [...]}. Kept separate from run() so the
    matching LOGIC (not the network calls) is directly unit-testable
    against a fake search_fn."""
    brand = brands_by_name_lower.get(entry["brand"].lower())
    if brand is None:
        return {"status": "no_brand"}

    # NAME_OVERRIDES swaps in the catalog's real product.name for a known
    # handful of chart entries whose own name text doesn't match it
    # byte-for-byte (see NAME_OVERRIDES's own comment) -- search still
    # casts its net with the original chart name (ilike substring, so it
    # finds the real product either way), only the exact-match comparison
    # target changes.
    match_name = NAME_OVERRIDES.get((entry["brand"], entry["name"]), entry["name"])

    candidates = search_fn(brand["id"], entry["name"])
    exact = [c for c in candidates if c["name"].strip().lower() == match_name.strip().lower()]

    if len(exact) == 1:
        return {"status": "matched", "product": exact[0]}
    if len(exact) == 0:
        return {"status": "no_match", "candidates": candidates}
    return {"status": "ambiguous", "candidates": exact}


def run(admin_api_url: str, token: str, chart_entries: list = None, list_brands_fn=None,
        search_fn=None, set_position_fn=None) -> dict:
    """Tolerates per-entry errors, same principle as every other batch
    script in this project. Injectable fns let tests supply fakes instead
    of hitting the network."""
    chart_entries = chart_entries if chart_entries is not None else load_chart_entries()
    list_brands_ = list_brands_fn if list_brands_fn is not None else lambda: list_brands(admin_api_url, token)
    search = search_fn if search_fn is not None else lambda brand_id, name: search_products(admin_api_url, token, brand_id, name)
    set_position = set_position_fn if set_position_fn is not None else lambda pid, oil, motion: set_plotter_position(admin_api_url, token, pid, oil, motion)

    brands = list_brands_()
    brands_by_name_lower = {b["name"].lower(): b for b in brands}

    matched = 0
    no_brand = 0
    no_match = 0
    ambiguous = 0
    errors = 0

    for entry in chart_entries:
        try:
            result = match_entry(entry, brands_by_name_lower, search)
            if result["status"] == "matched":
                product = result["product"]
                set_position(product["id"], entry["oil"], entry["motion"])
                matched += 1
                logger.info("Matched %r (%s) -> product_id=%s", entry["name"], entry["brand"], product["id"])
            elif result["status"] == "no_brand":
                no_brand += 1
                logger.warning("No brand found matching %r -- skipping %r", entry["brand"], entry["name"])
            elif result["status"] == "no_match":
                no_match += 1
                names = [c["name"] for c in result["candidates"]]
                logger.warning("No exact match for %r (%s) -- needs manual review. Search candidates: %s",
                                entry["name"], entry["brand"], names or "none")
            else:
                ambiguous += 1
                ids = [c["id"] for c in result["candidates"]]
                logger.warning("Multiple exact matches for %r (%s) -- needs manual review. product_ids: %s",
                                entry["name"], entry["brand"], ids)
        except Exception:
            errors += 1
            logger.exception("Failed processing chart entry %r (%s) -- will retry next run",
                              entry.get("name"), entry.get("brand"))

    return {
        "total": len(chart_entries), "matched": matched, "no_brand": no_brand,
        "no_match": no_match, "ambiguous": ambiguous, "errors": errors,
    }


def main():
    admin_api_url = os.environ.get("ADMIN_API_URL")
    token = os.environ.get("ADMIN_API_TOKEN")
    if not admin_api_url or not token:
        logger.error("ADMIN_API_URL and ADMIN_API_TOKEN must both be set -- see this script's module docstring for setup.")
        sys.exit(1)

    summary = run(admin_api_url, token)
    logger.info("Done: %s", summary)
    logger.info("%d/%d chart entries matched and written; %d need manual review (no_match=%d, ambiguous=%d, no_brand=%d)",
                summary["matched"], summary["total"],
                summary["no_match"] + summary["ambiguous"] + summary["no_brand"],
                summary["no_match"], summary["ambiguous"], summary["no_brand"])

    if summary["total"] > 0 and summary["matched"] == 0 and summary["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
