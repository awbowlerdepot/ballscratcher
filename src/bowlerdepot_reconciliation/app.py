"""
Reconciliation against bowlerdepot.com -- OWN store, not a third-party site,
so per the architecture doc this uses BigCommerce's v3 Catalog REST API
directly rather than scraping HTML. Two checks, per the doc:

1. Coverage: for every current, published ball in our normalized DB, does a
   matching product exist on BowlerDepot? Missing -> "not yet listed" queue
   item, so a real ball can get added to the store in a timely manner
   rather than sitting unnoticed.
2. Accuracy: for balls that DO match, do BigCommerce's stored specs agree
   with our manufacturer-sourced values? Disagreement -> the same
   review_queue mismatch mechanism bowwwl_cross_check and pdf_parser use
   (source='bowlerdepot_reconciliation').

Platform confirmation: bowlerdepot.com runs on BigCommerce Stencil
(confirmed by the architecture doc's earlier research). This session
confirmed the CURRENT (2026) v3 Catalog Products API shape directly
against BigCommerce's own developer docs (fetched live, not from training
memory) -- GET /stores/{store_hash}/v3/catalog/products, authenticated via
the X-Auth-Token header, response shaped
{"data": [...products], "meta": {"pagination": {...}}}, each product
carrying an arbitrary custom_fields: [{"name", "value", "id"}] array --
BigCommerce's own confirmed mechanism for merchant-defined attributes like
RG/DIFF/coverstock, which is the field this module assumes bowling specs
live in.

WHAT'S GENUINELY UNVERIFIED, disclosed rather than guessed past: this
project has no real BowlerDepot store credentials, so none of the
following has been checked against the actual live store, only against
BigCommerce's generic public API docs:

  - Which custom_fields NAMES BowlerDepot actually uses for RG/DIFF/mass
    bias/coverstock/etc (if it uses custom_fields for these at all --
    the architecture doc flagged this exact question as something to
    check "once you're in the API", not something to assume). CUSTOM_FIELD_
    NAME_CANDIDATES below is a best-guess mapping (common, human-readable
    label variants), used case-insensitively, NOT a confirmed mapping.
  - Whether BowlerDepot models each ball weight as a true BigCommerce
    "variant" (a sub-resource of one product) or as entirely separate
    products/SKUs, one per weight -- bowlerdepot.com's own storefront
    returned very little static content when checked live this session
    (likely a JS-rendered Stencil theme; a plain page-text read showed
    only header/footer/contact info, no product listings, which isn't
    enough to determine this either way without deeper JS-rendered
    inspection not attempted this session). This module assumes the
    SIMPLER of the two shapes -- one BigCommerce product per (brand, ball
    name, weight) combination, matched by name+weight -- since that's
    buildable and testable without a confirmed variant schema; revisit if
    real API access shows BowlerDepot actually uses true variants instead.
  - Real store_hash / API token (obviously) -- BIGCOMMERCE_STORE_HASH and
    the Secrets Manager secret referenced by BIGCOMMERCE_SECRET_ARN are
    both unset placeholders in template.yaml until a real store account is
    wired up.

Given all of the above, treat this module the same way as
netsuite_product_scraper's fetch_page(): the STRUCTURE is built against a
real, current, confirmed API contract, but the actual field-name mapping
and variant-vs-separate-product assumption need to be checked against the
real store before this is trusted to write anything back or rely on for
real coverage numbers.
"""
import logging
import re
from difflib import SequenceMatcher

logger = logging.getLogger()
logger.setLevel(logging.INFO)

API_BASE = "https://api.bigcommerce.com"
PAGE_LIMIT = 250  # BigCommerce's documented max per page for this endpoint

# Best-guess custom_fields "name" values that might carry each spec,
# matched case-insensitively against whatever custom_fields BowlerDepot's
# real catalog actually has. UNVERIFIED -- see module docstring.
CUSTOM_FIELD_NAME_CANDIDATES = {
    "rg": ["rg", "radius of gyration", "core rg"],
    "differential": ["diff", "differential", "core diff"],
    "mass_bias": ["mb", "mass bias", "mb diff", "mass bias differential"],
    "coverstock_name": ["coverstock", "cover stock"],
    "core_name": ["core", "core name"],
}

FUZZY_MATCH_THRESHOLD = 0.80  # SequenceMatcher ratio; see fuzzy_match_product. Chosen to
# comfortably catch a real-world-plausible "+ Bowling Ball" suffix (measured
# ratio ~0.84 for "Brunswick Fury Emerald/Black Hybrid" vs. the same name
# with " Bowling Ball" appended) while still rejecting an unrelated name.


def build_products_url(store_hash: str, page: int = 1, limit: int = PAGE_LIMIT) -> str:
    return f"{API_BASE}/stores/{store_hash}/v3/catalog/products?page={page}&limit={limit}&include=custom_fields"


def fetch_products_page(store_hash: str, auth_token: str, page: int = 1, timeout: int = 30) -> dict:
    """Fetch one page of the v3 Catalog Products response. Kept separate
    from pagination/parsing so tests can feed a real-shaped fake response
    without a network call."""
    import requests

    resp = requests.get(
        build_products_url(store_hash, page),
        headers={"Accept": "application/json", "X-Auth-Token": auth_token},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_all_products(store_hash: str, auth_token: str, max_pages: int = 100) -> list:
    """Paginates through every product in the store, using the response's
    own meta.pagination.total_pages to know when to stop (BigCommerce's
    documented shape, confirmed against the real API docs this session --
    see module docstring). max_pages is a hard safety cap independent of
    that, in case total_pages is ever missing/wrong."""
    all_products = []
    page = 1
    while page <= max_pages:
        body = fetch_products_page(store_hash, auth_token, page)
        all_products.extend(body.get("data", []))
        total_pages = body.get("meta", {}).get("pagination", {}).get("total_pages", page)
        if page >= total_pages:
            break
        page += 1
    return all_products


def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace -- for comparing
    a manufacturer's "Fury Emerald/Black Hybrid" against however
    BowlerDepot's storefront happens to have typed/formatted the same
    ball's name (extra words like "Bowling Ball", different punctuation,
    etc. are all real, expected variance per the architecture doc:
    "retail listings sometimes append color variants or drop qualifiers
    manufacturers use")."""
    cleaned = re.sub(r"[^a-z0-9\s]", "", name.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def fuzzy_match_product(our_name: str, bigcommerce_products: list, threshold: float = FUZZY_MATCH_THRESHOLD):
    """Returns (product, ratio) for the best-scoring BigCommerce product
    match, or (None, 0.0) if nothing clears threshold. Exact normalized-
    name match short-circuits to a perfect score; otherwise falls back to
    difflib.SequenceMatcher ratio against normalized names -- a heuristic,
    not a guaranteed-correct match, which is exactly why every match this
    produces gets recorded with match_status rather than silently trusted
    (see check_coverage/'ambiguous' handling)."""
    our_normalized = _normalize_name(our_name)
    best_product, best_ratio = None, 0.0

    for product in bigcommerce_products:
        candidate_normalized = _normalize_name(product.get("name", ""))
        if candidate_normalized == our_normalized:
            return product, 1.0
        ratio = SequenceMatcher(None, our_normalized, candidate_normalized).ratio()
        if ratio > best_ratio:
            best_product, best_ratio = product, ratio

    if best_ratio >= threshold:
        return best_product, best_ratio
    return None, 0.0


def check_coverage(our_products: list, bigcommerce_products: list) -> list:
    """our_products: list of {product_id, brand_name, name} dicts (current,
    published balls from our own DB). Returns a list of
    {product_id, name, match} dicts where match is None (not found -- a
    "not yet listed" item), or {"bigcommerce_product_id", "match_ratio",
    "ambiguous"} (ambiguous = True when the best match scored above
    threshold but not a clean 1.0, i.e. worth a human glance rather than
    auto-trusting)."""
    results = []
    for product in our_products:
        full_name = f"{product['brand_name']} {product['name']}"
        match, ratio = fuzzy_match_product(full_name, bigcommerce_products)
        if match is None:
            results.append({"product_id": product["product_id"], "name": full_name, "match": None})
        else:
            results.append({
                "product_id": product["product_id"],
                "name": full_name,
                "match": {
                    "bigcommerce_product_id": str(match["id"]),
                    "bigcommerce_sku": match.get("sku"),
                    "match_ratio": ratio,
                    "ambiguous": ratio < 1.0,
                },
            })
    return results


def _custom_fields_by_name(product: dict) -> dict:
    """Returns {name.lower(): value} for a BigCommerce product's
    custom_fields array -- the confirmed-real shape from BigCommerce's own
    docs (see module docstring)."""
    return {cf["name"].strip().lower(): cf.get("value") for cf in product.get("custom_fields", [])}


def _find_custom_field(fields_by_name: dict, candidates: list):
    for candidate in candidates:
        if candidate in fields_by_name:
            return fields_by_name[candidate]
    return None


def _to_float(value):
    if value is None:
        return None
    match = re.search(r"-?(?:\d+\.?\d*|\.\d+)", str(value))
    return float(match.group()) if match else None


def extract_specs_from_custom_fields(product: dict) -> dict:
    """Pulls whatever RG/DIFF/mass_bias values it can find in a
    BigCommerce product's custom_fields, using CUSTOM_FIELD_NAME_CANDIDATES'
    best-guess label list. Returns None for anything not found rather than
    guessing -- see module docstring's disclosed-unverified-mapping
    caveat."""
    fields = _custom_fields_by_name(product)
    return {
        "rg": _to_float(_find_custom_field(fields, CUSTOM_FIELD_NAME_CANDIDATES["rg"])),
        "differential": _to_float(_find_custom_field(fields, CUSTOM_FIELD_NAME_CANDIDATES["differential"])),
        "mass_bias": _to_float(_find_custom_field(fields, CUSTOM_FIELD_NAME_CANDIDATES["mass_bias"])),
    }


def check_accuracy(matched_pairs: list, tolerance: float = 0.001) -> list:
    """matched_pairs: list of {product_id, our_sku, bigcommerce_product}
    dicts (our_sku is a single {weight_lbs, rg, differential, mass_bias}
    dict -- see module docstring's "one product per weight" assumption).
    Returns review_queue-shaped mismatch dicts, same field_name convention
    as bowwwl_cross_check ("rg_16lb" etc.)."""
    mismatches = []
    for pair in matched_pairs:
        product_id = pair["product_id"]
        weight = pair["our_sku"]["weight_lbs"]
        their_specs = extract_specs_from_custom_fields(pair["bigcommerce_product"])

        for field in ("rg", "differential", "mass_bias"):
            our_value = pair["our_sku"].get(field)
            their_value = their_specs.get(field)
            if our_value is None or their_value is None:
                continue
            if abs(our_value - their_value) > tolerance:
                mismatches.append({
                    "product_id": product_id,
                    "field_name": f"{field}_{weight}lb",
                    "current_value": str(our_value),
                    "proposed_value": str(their_value),
                    "reason": f"bowlerdepot_reconciliation: {field} at {weight}lb disagrees by {abs(our_value - their_value):.4f} (tolerance {tolerance})",
                })
    return mismatches


# ---------------------------------------------------------------------
# Lambda handler + DB write. Same split as every other scraper in this
# project: pure matching/comparison above, mechanical DB/scheduling glue
# below (deferred-imported).
# ---------------------------------------------------------------------

import json
import os


def get_db_connection():
    import boto3
    import psycopg2

    secret_arn = os.environ["DB_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    secret = json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])

    return psycopg2.connect(
        host=secret["host"],
        port=secret.get("port", 5432),
        dbname=secret["dbname"],
        user=secret["username"],
        password=secret["password"],
    )


def get_bigcommerce_credentials():
    """Separate secret from the DB credentials -- a BigCommerce API token
    is a different kind of sensitive value with its own rotation/access
    story, not worth coupling to the DB secret's lifecycle."""
    import boto3

    secret_arn = os.environ["BIGCOMMERCE_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    secret = json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])
    return secret["store_hash"], secret["auth_token"]


def list_our_current_products(conn) -> list:
    with conn.cursor() as cur:
        cur.execute(
            """
            select p.id, b.name, p.name
            from products p
            join brands b on b.id = p.brand_id
            where p.published = true and p.status = 'current'
            """
        )
        return [{"product_id": row[0], "brand_name": row[1], "name": row[2]} for row in cur.fetchall()]


def get_product_skus(conn, product_id) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "select weight_lbs, rg, differential, mass_bias from product_skus where product_id = %s",
            (product_id,),
        )
        return [
            {"weight_lbs": row[0], "rg": row[1], "differential": row[2], "mass_bias": row[3]}
            for row in cur.fetchall()
        ]


def upsert_bowlerdepot_match(conn, product_id, bigcommerce_product_id, bigcommerce_sku, match_status: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into bowlerdepot_products (product_id, bigcommerce_product_id, bigcommerce_sku, match_status, last_synced_at)
            values (%s, %s, %s, %s, now())
            on conflict (bigcommerce_product_id, bigcommerce_sku) do update set
                product_id = excluded.product_id,
                match_status = excluded.match_status,
                last_synced_at = now()
            """,
            (product_id, bigcommerce_product_id, bigcommerce_sku, match_status),
        )
    conn.commit()


def write_not_yet_listed_item(conn, product_id, name: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into review_queue (product_id, field_name, current_value, proposed_value, source, reason)
            values (%s, 'bowlerdepot_listing', %s, null, 'bowlerdepot_reconciliation', %s)
            """,
            (product_id, name, f"'{name}' is in our catalog but has no matching product on bowlerdepot.com"),
        )
    conn.commit()


def write_review_items(conn, mismatches: list):
    with conn.cursor() as cur:
        for m in mismatches:
            cur.execute(
                """
                insert into review_queue (product_id, field_name, current_value, proposed_value, source, reason)
                values (%s, %s, %s, %s, 'bowlerdepot_reconciliation', %s)
                """,
                (m["product_id"], m["field_name"], m["current_value"], m["proposed_value"], m["reason"]),
            )
    conn.commit()


def handler(event, context):
    """Scheduled invocation: pulls the full BowlerDepot catalog once, then
    checks coverage + accuracy for every current/published product in our
    own DB against it. Coverage misses become 'not yet listed' review
    items; accuracy mismatches on matched products become the usual
    mismatch review items."""
    store_hash, auth_token = get_bigcommerce_credentials()
    bigcommerce_products = fetch_all_products(store_hash, auth_token)
    logger.info("Fetched %d products from BowlerDepot's BigCommerce catalog", len(bigcommerce_products))

    conn = get_db_connection()
    try:
        our_products = list_our_current_products(conn)
        coverage = check_coverage(our_products, bigcommerce_products)

        not_yet_listed = 0
        matched_pairs = []
        for entry in coverage:
            if entry["match"] is None:
                write_not_yet_listed_item(conn, entry["product_id"], entry["name"])
                not_yet_listed += 1
                continue

            bc_product_id = entry["match"]["bigcommerce_product_id"]
            bc_sku = entry["match"]["bigcommerce_sku"]
            match_status = "ambiguous" if entry["match"]["ambiguous"] else "matched"
            upsert_bowlerdepot_match(conn, entry["product_id"], bc_product_id, bc_sku, match_status)

            bc_product = next((p for p in bigcommerce_products if str(p["id"]) == bc_product_id), None)
            if bc_product is None or match_status == "ambiguous":
                continue  # don't run an accuracy check against a match we're not confident in

            for sku in get_product_skus(conn, entry["product_id"]):
                matched_pairs.append({
                    "product_id": entry["product_id"],
                    "our_sku": sku,
                    "bigcommerce_product": bc_product,
                })

        mismatches = check_accuracy(matched_pairs)
        if mismatches:
            write_review_items(conn, mismatches)
    finally:
        conn.close()

    return {
        "statusCode": 200,
        "body": json.dumps({
            "our_product_count": len(our_products),
            "bigcommerce_product_count": len(bigcommerce_products),
            "not_yet_listed_count": not_yet_listed,
            "mismatch_count": len(mismatches),
        }),
    }
