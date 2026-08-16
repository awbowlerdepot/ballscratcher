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

**Both of the two previously-unverified questions below were resolved in
a later session** by browsing the real, live public storefront (Claude in
Chrome) rather than the private v3 Catalog API -- still no real
store_hash/API token exists, so the API's exact JSON shape for these
fields remains unconfirmed, but the storefront directly shows what the
underlying custom_fields almost certainly are:

  - **Product-vs-variant modeling: CONFIRMED true BigCommerce variants,
    not separate products.** Real product pages (checked: Storm Alpha
    Crux, Roto Grip RST Hyperdrive) show ONE product page per ball with a
    "Weight" `<select>` offering 16/15/14/13/12 lbs as options -- this
    project's original "one BigCommerce product per weight" assumption
    was wrong. Conveniently, this doesn't actually break the matching
    logic below (`fuzzy_match_product()` already matches by ball name
    only, which was always weight-independent), but it does matter for
    `check_accuracy()` -- see the next point and that function's own
    docstring for the real consequence.
  - **Custom field names: CONFIRMED real display labels**, found in the
    rendered product page (not the private API, so the exact
    `custom_fields[].name` string is inferred from what's shown, not
    literally read from JSON): "Radius of Gyration(15lb)", "Max
    Differential(15lb)", "Int. Differential(15lb)" -- all three
    explicitly qualified with "(15lb)", confirmed identical on both real
    products checked. Two of the three original best-guess candidates
    ("Diff"/"Differential" for differential, "MB"/"Mass Bias" for mass
    bias) were simply wrong -- BowlerDepot's own real terminology for
    mass bias is "Int. Differential" (the same term MOTIV's real site
    uses, confirmed elsewhere in this project -- possibly a shared
    industry convention rather than coincidence). CUSTOM_FIELD_NAME_
    CANDIDATES below has been updated with the real values, and
    `_find_custom_field()`'s matching was loosened from exact-match to
    prefix-match (startswith, not a bare substring check) specifically
    because it's not certain whether "(15lb)" is really part of the
    stored custom_fields.name value or added by the storefront template
    -- prefix matching handles either case without guessing which, and
    without the false-positive risk a bare substring check would have
    (e.g. a short fallback candidate like "rg" matching some unrelated
    field that merely contains those letters, like "Target Weight").
  - **The real, important consequence of both findings together:**
    BowlerDepot only publishes ONE spec value per ball (qualified
    "15lb"), not one per weight, because weights are variants of a single
    product with one shared custom_fields set. `check_accuracy()` was
    fixed to only compare our 15lb-weight SKU against BowlerDepot's
    values -- comparing every one of our weights (16lb, 14lb, etc.)
    against BowlerDepot's single 15lb-reference number would have
    produced constant false-positive mismatches for every non-15lb
    weight, since RG/DIFF genuinely differ by weight on a real ball (this
    project's own fixtures already prove that). This was caught and
    fixed before ever running against a real store, not after.
  - Real store_hash / API token (still not obtained) -- BIGCOMMERCE_
    SECRET_ARN is still an unset placeholder in template.yaml until a
    real store account is wired up. That's the one thing left that
    genuinely can't be checked without it: whether the API's JSON
    actually shapes these fields the way the storefront's rendering
    implies.

Treat this module the same way as netsuite_product_scraper's fetch_page():
the STRUCTURE is built against a real, current, confirmed API contract,
and the field-name mapping and variant-vs-product question are now
resolved against real (if indirect) evidence -- but the exact API
response shape for custom_fields still needs a real store to fully
confirm before trusting this module's output blindly.
"""
import logging
import re
from difflib import SequenceMatcher

logger = logging.getLogger()
logger.setLevel(logging.INFO)

API_BASE = "https://api.bigcommerce.com"
PAGE_LIMIT = 250  # BigCommerce's documented max per page for this endpoint

# custom_fields "name" values that might carry each spec, matched
# case-insensitively (and by substring, not exact match -- see
# _find_custom_field) against whatever custom_fields BowlerDepot's real
# catalog actually has. The first three entries in each list are the real
# display labels confirmed live on bowlerdepot.com's storefront this
# session (Storm Alpha Crux, Roto Grip RST Hyperdrive), including the
# real "(15lb)" qualifier BowlerDepot always attaches -- see module
# docstring. The remaining entries are the original best-guess fallbacks,
# kept in case a different product template omits the qualifier or uses
# slightly different wording.
CUSTOM_FIELD_NAME_CANDIDATES = {
    "rg": ["radius of gyration(15lb)", "radius of gyration", "rg", "core rg"],
    "differential": ["max differential(15lb)", "max differential", "diff", "differential", "core diff"],
    "mass_bias": ["int. differential(15lb)", "int. differential", "mb", "mass bias", "mb diff", "mass bias differential"],
    "coverstock_name": ["coverstock", "cover stock"],
    "core_name": ["core", "core name"],
}

# BowlerDepot's real product pages publish exactly one spec value per
# ball, qualified "(15lb)" -- not one per weight variant, since (also
# confirmed this session) weight is a BigCommerce variant/option on a
# single product, not a separate product per weight. check_accuracy()
# below only compares against this weight for exactly that reason: our
# other weights' real RG/DIFF values are expected to differ from the
# 15lb reference number, so comparing them would be a false-positive
# mismatch, not a real one.
BOWLERDEPOT_REFERENCE_WEIGHT_LBS = 15

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


# Real incident, Al: "it is finding the 'Storm iQ Tour AI' instead of the
# 'Storm iQ Tour'" -- fuzzy_match_product's old SequenceMatcher-only
# scoring is a character-level ratio, and a short appended word barely
# dents it regardless of what that word is: "storm iq tour" vs "storm iq
# tour ai" scores ~0.90, comfortably clearing FUZZY_MATCH_THRESHOLD.
# Bowling-ball naming convention is overwhelmingly "base name + a short
# suffix that names a genuinely DIFFERENT variant/ball" (Solid/Pearl/
# Hybrid/Pro/AI/etc.), so a metric that can't tell "harmless retail
# suffix" apart from "different product's suffix" will keep silently
# matching the wrong ball to a real product's name.
#
# _GENERIC_NAME_SUFFIX_TOKENS is the one narrow exception carved back
# out: the "+ Bowling Ball" suffix is the specific case FUZZY_MATCH_
# THRESHOLD's own calibration comment already documented as intentional,
# real-world-observed variance (see below) -- a purely generic retail
# label, not a qualifier that ever names a different ball. Any OTHER
# extra/missing word between two names is now a hard reject at the
# candidate-selection stage, before SequenceMatcher ever gets a ratio to
# score, so it can't be talked into a false match by favorable character
# overlap the way "ai" was.
_GENERIC_NAME_SUFFIX_TOKENS = {"bowling", "ball", "balls"}


def _loose_tokens(name: str) -> set:
    """Word-boundary-aware tokenization used ONLY by the token-
    compatibility gate below -- deliberately a different normalization
    from _normalize_name. _normalize_name strips punctuation entirely
    (e.g. "Emerald/Black" -> "emeraldblack", one merged word) so
    SequenceMatcher can still score a close character-level match despite
    a dropped slash; but that same stripping would make a real
    punctuation-only difference look like a word-count mismatch to a
    token-set comparison and wrongly reject a good match (see
    test_fuzzy_match_high_but_not_exact_when_slash_becomes_a_space).
    Here, punctuation is replaced with a space instead of deleted, so
    "Emerald/Black", "Emerald-Black", and "Emerald Black" all tokenize
    identically to {"emerald", "black"}."""
    spaced = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    return set(spaced.split())


def _names_token_compatible(our_name: str, candidate_name: str) -> bool:
    """True when our_name and candidate_name differ, if at all, only by
    words in _GENERIC_NAME_SUFFIX_TOKENS on either side. fuzzy_match_
    product uses this as a hard gate BEFORE computing a SequenceMatcher
    ratio at all -- a candidate that fails this check is never a match
    candidate, no matter how high its character-similarity ratio scores,
    since a real distinguishing word (any word outside the generic-filler
    set) is this project's strongest available signal that two names
    refer to genuinely different balls, not typo/formatting variance of
    the same one."""
    extra = _loose_tokens(our_name) ^ _loose_tokens(candidate_name)
    return extra.issubset(_GENERIC_NAME_SUFFIX_TOKENS)


def fuzzy_match_product(our_name: str, bigcommerce_products: list, threshold: float = FUZZY_MATCH_THRESHOLD):
    """Returns (product, ratio) for the best-scoring BigCommerce product
    match, or (None, 0.0) if nothing clears threshold. Exact normalized-
    name match short-circuits to a perfect score; otherwise falls back to
    difflib.SequenceMatcher ratio against normalized names -- a heuristic,
    not a guaranteed-correct match, which is exactly why every match this
    produces gets recorded with match_status rather than silently trusted
    (see check_coverage/'ambiguous' handling).

    Every non-exact candidate must also pass _names_token_compatible
    before its ratio is even computed -- see that function's own
    docstring for the real incident (a distinguishing suffix like "AI")
    this gate exists to block. A candidate that fails is simply skipped,
    same as if it scored 0.0; it can never win best_ratio no matter how
    high its raw character-similarity would have been."""
    our_normalized = _normalize_name(our_name)
    best_product, best_ratio = None, 0.0

    for product in bigcommerce_products:
        candidate_name = product.get("name", "")
        candidate_normalized = _normalize_name(candidate_name)
        if candidate_normalized == our_normalized:
            return product, 1.0
        if not _names_token_compatible(our_name, candidate_name):
            continue
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
    """Prefix match, not exact-only -- deliberately, since it's not
    certain whether BowlerDepot's real custom_fields.name values literally
    include the "(15lb)" qualifier seen on the storefront or whether
    that's added by the display template (see module docstring). A
    candidate like "radius of gyration" matches either "radius of
    gyration" or "radius of gyration(15lb)" via startswith().

    Deliberately startswith(), not a bare substring check (`candidate in
    field_name`): a short fallback candidate like "rg" or "mb" as a plain
    substring could false-positive-match an unrelated real field name
    that merely happens to contain those letters somewhere in the middle
    (e.g. "Target Weight" contains "rg"; "Thumb Hole" contains "mb").
    startswith() avoids that whole class of mismatch while still handling
    the one real pattern this session confirmed (a qualifier suffix)."""
    for candidate in candidates:
        for field_name, value in fields_by_name.items():
            if field_name.startswith(candidate):
                return value
    return None


def _to_float(value):
    if value is None:
        return None
    match = re.search(r"-?(?:\d+\.?\d*|\.\d+)", str(value))
    return float(match.group()) if match else None


def extract_specs_from_custom_fields(product: dict) -> dict:
    """Pulls whatever RG/DIFF/mass_bias values it can find in a
    BigCommerce product's custom_fields, using CUSTOM_FIELD_NAME_CANDIDATES.
    These are BowlerDepot's real, confirmed-live display labels (see
    module docstring) for the single 15lb-reference value the storefront
    publishes -- not a per-weight breakdown, since weight is a variant of
    one product, not a separate product/custom_fields set per weight.
    Returns None for anything not found rather than guessing."""
    fields = _custom_fields_by_name(product)
    return {
        "rg": _to_float(_find_custom_field(fields, CUSTOM_FIELD_NAME_CANDIDATES["rg"])),
        "differential": _to_float(_find_custom_field(fields, CUSTOM_FIELD_NAME_CANDIDATES["differential"])),
        "mass_bias": _to_float(_find_custom_field(fields, CUSTOM_FIELD_NAME_CANDIDATES["mass_bias"])),
    }


def check_accuracy(matched_pairs: list, tolerance: float = 0.001) -> list:
    """matched_pairs: list of {product_id, our_sku, bigcommerce_product}
    dicts (our_sku is a single {weight_lbs, rg, differential, mass_bias}
    dict). Returns review_queue-shaped mismatch dicts, same field_name
    convention as bowwwl_cross_check ("rg_15lb" etc.).

    Only ever compares our BOWLERDEPOT_REFERENCE_WEIGHT_LBS (15lb) SKU
    against BowlerDepot's values -- confirmed live this session that
    BowlerDepot's real product pages publish exactly one spec value per
    ball, explicitly qualified "(15lb)", not a value per weight. Comparing
    any other weight's real RG/DIFF against that single 15lb reference
    would produce a false-positive mismatch on every non-15lb weight (RG/
    DIFF genuinely differ by weight -- this project's own real fixtures
    already prove that), not a real disagreement worth a human's time.
    Callers are still free to pass in matched_pairs for every weight (the
    Lambda handler does, for simplicity); this function silently skips
    any weight that isn't the reference one rather than requiring the
    caller to pre-filter."""
    mismatches = []
    for pair in matched_pairs:
        weight = pair["our_sku"]["weight_lbs"]
        if weight != BOWLERDEPOT_REFERENCE_WEIGHT_LBS:
            continue

        product_id = pair["product_id"]
        their_specs = extract_specs_from_custom_fields(pair["bigcommerce_product"])

        for field in ("rg", "differential", "mass_bias"):
            our_value = pair["our_sku"].get(field)
            their_value = their_specs.get(field)
            if our_value is None or their_value is None:
                continue
            # our_value comes from get_product_skus() -> Postgres numeric
            # column -> decimal.Decimal via psycopg2; their_value comes from
            # extract_specs_from_custom_fields() -> _to_float() parsing
            # BigCommerce's custom_fields string values -> plain float. Same
            # real bug as pdf_parser's find_mismatches() and
            # bowwwl_cross_check's compare_to_our_data() (both found via
            # live CloudWatch logs earlier in this deploy): Decimal and
            # float can't be subtracted directly. Fixed here proactively,
            # before this function's daily schedule ever ran against a real
            # store, rather than waiting for the same crash a third time.
            # Coerce both to float for the comparison only --
            # str(our_value)/str(their_value) below still use the original
            # values, so no precision lost in what's written to
            # review_queue.
            if abs(float(our_value) - float(their_value)) > tolerance:
                mismatches.append({
                    "product_id": product_id,
                    "field_name": f"{field}_{weight}lb",
                    "current_value": str(our_value),
                    "proposed_value": str(their_value),
                    "reason": f"bowlerdepot_reconciliation: {field} at {weight}lb disagrees by {abs(float(our_value) - float(their_value)):.4f} (tolerance {tolerance})",
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
