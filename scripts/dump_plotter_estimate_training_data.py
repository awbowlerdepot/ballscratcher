#!/usr/bin/env python3
"""
One-time data-pull for refitting estimate_oil_motion (src/public_api/
service.py, duplicated into admin_api/service.py and all five scraper
Lambdas) against REAL data instead of the hand-tuned, never-validated
constants it currently ships with.

Real ask, Al: "i feel like it is way off for most balls". Backed up by
DEPLOY_RUNBOOK.md 6m's 2026-08-12 spot-check: across the 32 products
with a real Brunswick-chart position, estimate_oil_motion only landed an
exact oil match 2/32 times (mean absolute error 3.3 on the 1-16 scale)
and an exact motion match 3/32 times (MAE 2.8 on the 1-18 scale) -- worst
on asymmetric reactive-resin solids, where the flat "+3 solid" oil
adjustment overshoots hard (e.g. Revenge Solid: real oil 3, estimated
13).

That spot-check was ad-hoc and never saved. This script redoes it as a
reusable, shareable dump: for every product with oil_motion_source=
'chart' (an authoritative, hand-digitized-from-Brunswick's-own-PDF real
answer -- see backfill_plotter_chart_positions.py), pull its real
oil/motion actuals AND the same inputs estimate_oil_motion consumes
(core_type, coverstock_type, coverstock_material, has_particle, and a
reference SKU's differential, same 15lb-preferred convention as
_reference_sku/_reference_differential in every scraper's own estimate
hook). Writes one JSON object per line to stdout (or --out FILE),
{"product_id", "brand_name", "name", "actual_oil", "actual_motion",
"core_type", "coverstock_type", "coverstock_material", "has_particle",
"differential"} -- everything needed to refit the formula's constants
against real answers instead of guessing.

WHY THIS RUNS AGAINST BOTH APIS, NOT JUST ONE: the chart-matched id list
comes from public_api's GET /products/plotter (unauthenticated, no admin
token needed -- see consumer-site/.env.example's own comment on this),
called once for status=current and once for status=retired since a
chart position can exist on either and that endpoint's status filter
isn't an "all" option. The per-product core/coverstock/differential
detail then comes from admin_api's GET /products/{id} (p.* plus the
joined core_type -- see that function's own docstring), which needs the
usual bearer token. No new backend endpoint needed for either half.

I (the agent) can't reach either live API from this sandbox -- the shell
proxy blocks the API Gateway domain by allowlist. Al, please run this
yourself and share the output file back (paste it, or drop it in the
outputs folder) so the real constants can be refit against real data
rather than guessed at a second time.

Usage:
    export ADMIN_API_URL="https://<your-admin-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    export PUBLIC_API_URL="https://<your-public-api-id>.execute-api.us-west-1.amazonaws.com"
    python3 scripts/dump_plotter_estimate_training_data.py --out /tmp/plotter_training_data.jsonl
"""
import argparse
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("dump_plotter_estimate_training_data")

# Same retry posture as every other script in this project -- see
# backfill_core_ids.get_requests_session's docstring for the full story.
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
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def list_chart_matched_positions(public_api_url: str, session=None) -> list:
    """Both statuses -- a chart-matched product could be retired now even
    though the chart itself is a snapshot in time. Deduped by id in case
    a product's status changed between the two calls (shouldn't happen in
    the few seconds this takes, but cheap to guard)."""
    session = session if session is not None else get_requests_session()
    seen = {}
    for status in ("current", "retired"):
        resp = session.get(f"{public_api_url}/products/plotter", params={"status": status}, timeout=30)
        resp.raise_for_status()
        for item in resp.json().get("items", []):
            if item.get("oil_motion_source") == "chart":
                seen[item["id"]] = item
    return list(seen.values())


def get_product_detail(admin_api_url: str, token: str, product_id: str, session=None) -> dict:
    session = session if session is not None else get_requests_session()
    resp = session.get(
        f"{admin_api_url}/products/{product_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def reference_sku(skus: list) -> dict:
    """Byte-for-byte the same 15lb-preferred convention as every scraper's
    own _reference_sku (see e.g. product_scraper.app._reference_sku) --
    keeping this identical matters: the whole point is to reproduce
    exactly the differential value estimate_oil_motion would actually see
    at scrape time for this product, not just any SKU's differential."""
    if not skus:
        return None
    for sku in skus:
        if sku.get("weight_lbs") == 15 and sku.get("differential") is not None:
            return sku
    with_diff = [s for s in skus if s.get("differential") is not None]
    if not with_diff:
        return None
    return max(with_diff, key=lambda s: s.get("weight_lbs") or 0)


def build_training_row(entry: dict, detail: dict) -> dict:
    ref_sku = reference_sku(detail.get("skus") or [])
    return {
        "product_id": detail["id"],
        "brand_name": detail.get("brand_name"),
        "name": detail.get("name"),
        "actual_oil": entry.get("oil"),
        "actual_motion": entry.get("motion"),
        "core_type": detail.get("core_type"),
        "coverstock_type": detail.get("coverstock_type"),
        "coverstock_material": detail.get("coverstock_material"),
        "has_particle": detail.get("has_particle"),
        "differential": ref_sku.get("differential") if ref_sku else None,
    }


def run(admin_api_url: str, admin_token: str, public_api_url: str,
        list_positions_fn=None, get_detail_fn=None) -> list:
    list_positions = list_positions_fn if list_positions_fn is not None else lambda: list_chart_matched_positions(public_api_url)
    get_detail = get_detail_fn if get_detail_fn is not None else lambda pid: get_product_detail(admin_api_url, admin_token, pid)

    rows = []
    errors = 0
    for entry in list_positions():
        try:
            detail = get_detail(entry["id"])
            if detail is None:
                logger.warning("Chart-matched product %s (%s) has no detail -- skipping", entry["id"], entry.get("name"))
                continue
            rows.append(build_training_row(entry, detail))
        except Exception:
            errors += 1
            logger.exception("Failed pulling detail for product %s (%s)", entry.get("id"), entry.get("name"))

    logger.info("Pulled %d training rows (%d errors) out of chart-matched products", len(rows), errors)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=None, help="Write JSONL here instead of stdout")
    args = parser.parse_args()

    admin_api_url = os.environ.get("ADMIN_API_URL")
    admin_token = os.environ.get("ADMIN_API_TOKEN")
    public_api_url = os.environ.get("PUBLIC_API_URL")
    if not admin_api_url or not admin_token or not public_api_url:
        logger.error("ADMIN_API_URL, ADMIN_API_TOKEN, and PUBLIC_API_URL must all be set -- see this script's module docstring for setup.")
        sys.exit(1)

    rows = run(admin_api_url, admin_token, public_api_url)

    out = open(args.out, "w") if args.out else sys.stdout
    try:
        for row in rows:
            out.write(json.dumps(row) + "\n")
    finally:
        if args.out:
            out.close()
            logger.info("Wrote %d rows to %s", len(rows), args.out)


if __name__ == "__main__":
    main()
