"""
Business logic for the admin approval API, kept separate from the FastAPI
route layer in app.py so it's testable without fastapi/pydantic installed
(neither was installable in this sandbox -- pip's proxy returned 403 for
every attempt, same restriction noted in the other modules' README
caveats). Everything in this file is plain functions + a psycopg2
connection; app.py is a thin routing wrapper around it.

Covers the workflow the architecture doc decided on: review everything on
the initial catalog load, then steady-state auto-approve except mismatched
fields written to review_queue by the scraping functions (currently just
pdf_parser.sync_pdf_skus; bowwwl.com cross-check and BowlerDepot
reconciliation are meant to write into the same table later, not built
yet). Approving a review_queue row applies its proposed_value to the real
column it describes; rejecting leaves the stored value untouched.
"""
import re
from urllib.parse import urlparse

# review_queue.field_name convention, established by pdf_parser.sync_pdf_skus:
# a per-weight SKU field looks like "rg_16lb" / "differential_15lb" /
# "mass_bias_12lb"; anything else is treated as a product-level field name,
# but ONLY if it's in this whitelist -- field_name ultimately becomes a SQL
# column name, so an unrecognized value must be rejected rather than used
# directly (this is the injection guard, not string-escaping).
SKU_FIELD_NAME_RE = re.compile(r"^(rg|differential|mass_bias)_(\d{1,2})lb$")

PRODUCT_UPDATABLE_FIELDS = {
    "name", "color", "coverstock_material", "coverstock_type",
    "coverstock_name", "factory_finish", "part_number", "published",
}
# core_name removed (migration 007): it was never actually a products
# column -- only ball_families (now cores) ever had it, and that table was
# never wired up either, so this was a latent bug that would have 500'd
# execute_update_plan's f-string UPDATE if any review_queue row had ever
# actually carried field_name="core_name" (confirmed via grep that nothing
# ever wrote one -- bowwwl_cross_check explicitly excludes core from its
# comparable fields). Core info now lives on the cores table, joined in via
# get_product() below rather than being a directly-editable product field.

# Fields on product_skus that are numeric -- proposed_value is stored as
# text on review_queue (it has to represent values from multiple sources
# uniformly), so these need casting before being written back.
NUMERIC_SKU_FIELDS = {"rg", "differential", "mass_bias"}


def parse_review_field_name(field_name: str) -> dict:
    """Decides what a review_queue.field_name actually refers to. Returns
    {"scope": "sku", "column": "rg", "weight_lbs": 16} or
    {"scope": "product", "column": "color"}. Raises ValueError for
    anything not recognized -- deliberately fails closed rather than
    guessing, since this drives which SQL column gets written."""
    sku_match = SKU_FIELD_NAME_RE.match(field_name)
    if sku_match:
        return {"scope": "sku", "column": sku_match.group(1), "weight_lbs": int(sku_match.group(2))}

    if field_name in PRODUCT_UPDATABLE_FIELDS:
        return {"scope": "product", "column": field_name}

    raise ValueError(f"Unrecognized or non-updatable review_queue field_name: {field_name!r}")


def _cast_proposed_value(column: str, raw_value):
    if raw_value is None:
        return None
    if column in NUMERIC_SKU_FIELDS:
        return float(raw_value)
    if column == "published":
        return str(raw_value).strip().lower() in ("true", "t", "1", "yes")
    return raw_value


def build_update_plan(field_name: str, proposed_value) -> dict:
    """Pure decision of what to write where -- no DB access, fully testable.
    Returns a plan dict consumed by execute_update_plan(). Raises ValueError
    via parse_review_field_name for unrecognized fields."""
    parsed = parse_review_field_name(field_name)
    value = _cast_proposed_value(parsed["column"], proposed_value)

    if parsed["scope"] == "sku":
        return {
            "table": "product_skus",
            "column": parsed["column"],
            "value": value,
            "where": {"weight_lbs": parsed["weight_lbs"]},  # product_id added by caller
        }
    return {
        "table": "products",
        "column": parsed["column"],
        "value": value,
        "where": {},  # product_id (the row id itself) added by caller
    }


def execute_update_plan(cur, product_id: str, plan: dict) -> None:
    """Applies a build_update_plan() result via the given cursor. Column
    names are only ever drawn from SKU_FIELD_NAME_RE's fixed group or
    PRODUCT_UPDATABLE_FIELDS, both closed whitelists -- never from
    unsanitized user input -- so building the column name into the SQL
    string here is safe; the value itself is always parameterized."""
    if plan["table"] == "product_skus":
        cur.execute(
            f"update product_skus set {plan['column']} = %s, updated_at = now() "
            f"where product_id = %s and weight_lbs = %s",
            (plan["value"], product_id, plan["where"]["weight_lbs"]),
        )
    else:
        cur.execute(
            f"update products set {plan['column']} = %s, updated_at = now() where id = %s",
            (plan["value"], product_id),
        )


# ---------------------------------------------------------------------
# DB access. Deferred-imported psycopg2, mirroring the other functions --
# untested in this sandbox for the same reason noted in their READMEs
# (no Postgres instance available to actually run against). The functions
# above (parse_review_field_name, build_update_plan) are the part that's
# unit tested -- see tests/test_admin_api_service.py.
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


def resolve_caller_from_event(event: dict) -> dict:
    """Pulls the `context` object admin_api_authorizer's dual-mode
    handler() attaches to an authorized request (task #465-473, Al: "a
    more sophisticated admin spa... has users") back out of the raw
    Lambda event Mangum stores at `request.scope["aws.event"]` -- see
    app.py's get_caller() for the thin FastAPI-dependency wrapper around
    this. Kept here (not in app.py) rather than as inline dict-digging,
    same "app.py is a thin routing layer, service.py holds the actual
    logic" split this whole module's own header comment already commits
    to -- and it's what makes this pure event-shape logic unit-testable
    at all, since app.py's own routes can't be exercised in this sandbox
    (no fastapi install -- see that file's header comment).

    For an HTTP API v2 request with a Lambda REQUEST authorizer using the
    simple-response format, API Gateway places whatever `context` dict the
    authorizer returned at `event["requestContext"]["authorizer"]
    ["lambda"]` -- both authorizer modes (Cognito JWT and shared-secret,
    see admin_api_authorizer/app.py's own docstring) always return one on
    success, so in a real deployed request this path is never actually
    missing. Still defensive here (returns a safe fallback rather than
    raising) for two real cases: a request that somehow reached this code
    without going through the authorizer at all (shouldn't happen given
    AdminHttpApi's DefaultAuthorizer, but a KeyError here shouldn't be
    what surfaces to a caller if it ever did), and this module's own
    functions being called directly from a script/test with no event at
    all (`resolve_caller_from_event({})`). The fallback role is "admin",
    not "editor" -- nothing in this codebase yet actually GATES an action
    on role (see this function's own task's docstring: wiring identity
    through is task #468, per-route role enforcement is explicitly
    deferred future work), so defaulting to the more permissive role here
    doesn't newly restrict anything that worked before this function
    existed; it only affects what `resolved_by` defaults to."""
    authorizer_context = (
        (event or {}).get("requestContext", {}).get("authorizer", {}).get("lambda", {}) or {}
    )
    return {
        "resolved_by": authorizer_context.get("resolved_by", "unknown"),
        "role": authorizer_context.get("role", "admin"),
        "caller_type": authorizer_context.get("caller_type", "unknown"),
    }


def list_review_queue(conn, status: str = "pending", product_id: str = None, limit: int = 50, offset: int = 0) -> list:
    query = """
        select rq.id, rq.product_id, p.name as product_name, p.url as product_url,
               rq.field_name, rq.current_value, rq.proposed_value, rq.source,
               rq.reason, rq.status, rq.created_at, rq.resolved_at, rq.resolved_by
        from review_queue rq
        join products p on p.id = rq.product_id
        where rq.status = %s
    """
    params = [status]
    if product_id:
        query += " and rq.product_id = %s"
        params.append(product_id)
    query += " order by rq.created_at asc limit %s offset %s"
    params += [limit, offset]

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_review_item(conn, review_id: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            select rq.id, rq.product_id, p.name as product_name, p.url as product_url,
                   rq.field_name, rq.current_value, rq.proposed_value, rq.source,
                   rq.reason, rq.status, rq.created_at, rq.resolved_at, rq.resolved_by
            from review_queue rq
            join products p on p.id = rq.product_id
            where rq.id = %s
            """,
            (review_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cur.description]
        return dict(zip(columns, row))


def approve_review_item(conn, review_id: str, resolved_by: str) -> dict:
    """Applies the review item's proposed_value to the real column it
    describes, then marks the row approved. Raises ValueError (via
    build_update_plan) if field_name isn't recognized -- the row is left
    pending in that case so it doesn't silently vanish from the queue."""
    item = get_review_item(conn, review_id)
    if item is None:
        raise LookupError(f"No review_queue row with id {review_id}")
    if item["status"] != "pending":
        raise ValueError(f"review_queue row {review_id} is already {item['status']}, not pending")

    plan = build_update_plan(item["field_name"], item["proposed_value"])

    with conn.cursor() as cur:
        execute_update_plan(cur, item["product_id"], plan)
        cur.execute(
            "update review_queue set status = 'approved', resolved_at = now(), resolved_by = %s where id = %s",
            (resolved_by, review_id),
        )
    conn.commit()

    return {"review_id": review_id, "status": "approved", "applied": plan}


def reject_review_item(conn, review_id: str, resolved_by: str, reason: str = None) -> dict:
    """Marks the row rejected without touching the underlying data --
    the current stored value is presumed correct, the proposed one is
    discarded."""
    with conn.cursor() as cur:
        cur.execute("select status from review_queue where id = %s", (review_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No review_queue row with id {review_id}")
        if row[0] != "pending":
            raise ValueError(f"review_queue row {review_id} is already {row[0]}, not pending")

        note = f"Rejected: {reason}" if reason else "Rejected"
        cur.execute(
            "update review_queue set status = 'rejected', resolved_at = now(), resolved_by = %s, "
            "reason = coalesce(reason || ' | ', '') || %s where id = %s",
            (resolved_by, note, review_id),
        )
    conn.commit()
    return {"review_id": review_id, "status": "rejected"}


def get_pending_review_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("select count(*) from review_queue where status = 'pending'")
        return cur.fetchone()[0]


def list_brands(conn) -> list:
    """Real ask from Al: the Products tab's brand filter was a raw-UUID
    text box ("Brand ID (exact)") -- functional (list_products already
    took brand_id), but useless unless you already knew or looked up the
    UUID separately. This backs a real dropdown instead: every brand,
    name only needed to populate `<option>` tags, so no pagination/search/
    filtering here -- there are a dozen or so brands total, nowhere near
    enough to need it (contrast list_products/list_cores, which paginate
    because a product or core catalog can run into the hundreds)."""
    with conn.cursor() as cur:
        cur.execute("select id, name from brands order by name")
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


# Video-popularity ranking (Al's ask) and total daily movement across a
# product's SKUs used to each be computed HERE, as a correlated subquery
# constant (_POPULARITY_SCORE_SQL / _TOTAL_DAILY_MOVEMENT_SQL) run on
# every single list_products/get_dashboard_summary call. REAL PERFORMANCE
# INCIDENT (2026-09-06, see 030_materialized_product_scores.sql's own
# header comment for the full writeup): that was fine at a small catalog
# size, stopped being fine at this one -- list_products' now-removed
# `demand` CTE alone recomputed both formulas over the ENTIRE unfiltered
# catalog on every call just to rank demand_score, and get_dashboard_
# summary below recomputed them a further three times for its own KPI/
# top-10 queries. Both formulas (and demand_score's own percent_rank()
# blend, previously _DEMAND_SCORE_CTE here) now live in exactly one
# place: src/refresh_product_scores/app.py, which recomputes them once a
# day into real products.popularity_score/total_daily_movement/
# demand_score columns instead. This function and get_dashboard_summary
# below just read those columns now -- see products.popularity_score's
# own column comment (030) for the formula history if you're looking for
# it.
#
# DAILY_MOVEMENT_LOOKBACK_DAYS survives here (unlike the two SQL
# constants above) because the Dashboard's growing/shrinking-movement
# trend queries and get_catalog_daily_movement_history below still
# compute a genuinely different, inherently-live two-window comparison
# via _sku_daily_movement_cte -- that's not a duplicate of the
# materialized total_daily_movement column, so it wasn't touched by the
# 030 migration. refresh_product_scores/app.py keeps its OWN independent
# copy of this same constant for the materialized column's formula, same
# hand-synced-constant convention as everywhere else in this project
# (Al: "can we add the sum of the ADUs for each product to the main
# table" -- originally "Average Daily Units"/ADU; renamed 2026-09-06 to
# Daily Movement, same underlying number, see DEPLOY_RUNBOOK.md).
DAILY_MOVEMENT_LOOKBACK_DAYS = 30


def _sku_daily_movement_cte(min_days_ago: int, max_days_ago: int = 0) -> str:
    """Shared building block for the Dashboard's Days-of-Supply and daily-
    movement-trend (growing/shrinking) queries below -- Al: "can we add top
    10 days of supply skus descending so lowest number of days first... can
    we build something would show top 10 growth ADUs and top 10 shrinking
    ADUs by sku" (that ask predates the 2026-09-06 ADU->Daily Movement
    rename -- the underlying metric asked for here is unchanged). Same
    drops-only, >=2-readings-required, elapsed_days-based per-SKU rate the
    old _TOTAL_DAILY_MOVEMENT_SQL established (now removed -- see 030's
    header comment; the formula lives on in refresh_product_scores/app.py)
    and daily_movement_by_brand's own inline sku_daily_movement CTE
    establish, just parameterized by an arbitrary trailing window instead
    of a single hardcoded DAILY_MOVEMENT_LOOKBACK_DAYS -- the growing/
    shrinking queries need TWO different windows (the current
    DAILY_MOVEMENT_LOOKBACK_DAYS one, and a second
    DAILY_MOVEMENT_LOOKBACK_DAYS-to-2*DAILY_MOVEMENT_LOOKBACK_DAYS-days-ago
    "previous" one to compare it against), so a third hand-copied literal
    here would mean the exact silent-drift risk this project's hand-synced-
    constant comments elsewhere (POPULARITY_HALF_LIFE_DAYS,
    DAILY_MOVEMENT_LOOKBACK_DAYS itself) already warn about. One
    parameterized function instead -- still SQL text interpolation, not a
    shared Python module across services, so it doesn't conflict with that
    same no-shared-module reasoning.

    min_days_ago/max_days_ago are both "days before now" (max_days_ago=0
    meaning "up through right now"). The current window is
    (DAILY_MOVEMENT_LOOKBACK_DAYS, 0); the previous comparison window is
    (2*DAILY_MOVEMENT_LOOKBACK_DAYS, DAILY_MOVEMENT_LOOKBACK_DAYS) --
    non-overlapping, same length.

    Returns a parenthesized subquery of (product_sku_id, daily_movement) --
    daily_movement is NULL (not a bare division) whenever elapsed_days
    isn't > 0, same guarded-division convention the old
    _TOTAL_DAILY_MOVEMENT_SQL's `case when sku.elapsed_days > 0 then
    ... else 0 end` used (now removed, formula lives on in
    refresh_product_scores/app.py), just NULL instead of 0 here since these callers
    need to entirely exclude a SKU without a computable rate (0 would be a
    nonsensical "no growth" or "infinite days of supply" result), not fold
    it into a sum. Callers filter on `daily_movement is not null` /
    `daily_movement > 0` as needed and never divide by elapsed_days
    themselves."""
    bounds = f"psh.checked_at >= now() - interval '{min_days_ago} days'"
    if max_days_ago:
        bounds += f" and psh.checked_at < now() - interval '{max_days_ago} days'"
    return f"""(
        select eh.product_sku_id,
               case when eh.elapsed_days > 0 then eh.units_sold / eh.elapsed_days else null end as daily_movement
        from (
            select h.product_sku_id,
                   sum(case when h.delta < 0 then -h.delta else 0 end) as units_sold,
                   extract(epoch from (max(h.checked_at) - min(h.checked_at))) / 86400.0 as elapsed_days
            from (
                select psh.product_sku_id, psh.checked_at,
                       psh.quantity - lag(psh.quantity) over (partition by psh.product_sku_id order by psh.checked_at) as delta
                from product_sku_stock_history psh
                where psh.quantity is not null
                  and {bounds}
            ) h
            group by h.product_sku_id
            having count(*) >= 2
        ) eh
    )"""


# Common-sense sort options for the Products tab's "Sort" control -- Al's
# ask: "lets add some common sense sort options for both the admin and
# consumer UIs". Identical to public_api/service.py's copy (kept in sync
# by hand, same no-shared-module reasoning as POPULARITY_HALF_LIFE_DAYS
# above) -- see that copy's comment for why 'newest'/'oldest' use
# release_date (not created_at/updated_at) with an explicit `nulls last`
# in both directions.
_SORT_ORDER_BY = {
    "popularity": "p.popularity_score desc, p.id asc",
    "newest": "p.release_date desc nulls last, p.id asc",
    "oldest": "p.release_date asc nulls last, p.id asc",
    "name_asc": "p.name asc, p.id asc",
    "name_desc": "p.name desc, p.id asc",
    # Al: "can we add a sort to the admin ui products list for total
    # ADU" -- direct follow-up to the Total ADU column itself (see
    # _TOTAL_DAILY_MOVEMENT_SQL above; that quote predates the 2026-09-06
    # ADU->Daily Movement rename, same metric). No "nulls last" needed --
    # as of migration 030, products.total_daily_movement is `not null
    # default 0` (see that migration), so it's never actually null, just
    # possibly 0 for a product with no qualifying SKU readings.
    "total_daily_movement": "p.total_daily_movement desc, p.id asc",
    # See _DEMAND_SCORE_CTE below for what this actually was before
    # migration 030 materialized it as a real column. No "nulls last"
    # needed -- `not null default 0` (030), same as total_daily_movement
    # above.
    "demand_score": "p.demand_score desc, p.id asc",
}
_DEFAULT_ORDER_BY = "p.updated_at desc, p.id asc"

# Demand Score's formula (a 50/50 percent_rank() blend of popularity_
# score and total_daily_movement -- see products.demand_score's own
# column comment, migration 030, for the full "why a separate metric,
# why percentile rank, why 50/50" reasoning that used to live here) now
# lives solely in src/refresh_product_scores/app.py -- see this file's
# own comment above DAILY_MOVEMENT_LOOKBACK_DAYS for the real performance
# incident that moved it there. Nothing here computes it anymore;
# list_products/get_dashboard_summary below just read the materialized
# products.demand_score column.


def list_products(conn, published: bool = None, brand_id: str = None, search: str = None,
                   needs_video_summary_refresh: bool = None, has_approved_video_summaries: bool = None,
                   missing_core: bool = None, missing_coverstock: bool = None, missing_skus: bool = None,
                   html_fallback_skus: bool = None,
                   missing_video_candidates: bool = None,
                   source_platform: str = None,
                   status: str = None, sort: str = None,
                   limit: int = 50, offset: int = 0) -> list:
    """status: filters to products.status ('current' or 'retired' -- see
    migration 001's product_status enum). Al's direct ask after the
    Combat/display_order investigation: with five scrapers now writing
    status off each page's /current/ vs /retired/ URL path (product_
    scraper's STATUS_FROM_URL_RE, mirrored per-platform), he wants to
    filter the Products tab down to just current (or just retired)
    product lines rather than scrolling the whole catalog. No validation
    against the enum's two values here, same as source_platform below --
    an unrecognized value just matches zero rows rather than erroring,
    consistent with how every other string filter on this endpoint
    behaves.

    source_platform: filters to one scraper platform ('netsuite',
    'shopify', 'woocommerce', 'commercebuild', 'craft_cms' -- same values
    as products.source_platform and queue_rescrape's SCRAPE_QUEUE_ENV_VAR_
    BY_PLATFORM keys). Built for scripts/rescrape_netsuite_products.py
    (the MOTIV image-scoping fix's catalog-wide cleanup, see that
    module's docstring and netsuite_product_scraper's "SECOND real bug"
    section) -- brand_id alone would work too for a single-brand platform
    like MOTIV today, but source_platform is the more honest filter for
    "every product this specific scraper touches", robust to a platform
    someday having more than one brand (this module's own docstring
    already anticipates that for NetSuite: "MOTIV Bowling to start").

    needs_video_summary_refresh=True: products with at least one
    approved+summarized video, where video_reviews_summary is either
    still unset or stale relative to how many approved+summarized videos
    currently exist (video_reviews_summary_video_count is stored exactly
    for this comparison -- see 006_products_video_reviews_summary.sql).
    Built for scripts/backfill_video_review_rollups.py, but written as a
    general filter (not a one-off) since the same staleness can recur
    later -- a reassign/delete cleanup, or a product summarized before
    video_summarizer's automatic regeneration existed.

    has_approved_video_summaries=True: every product with at least one
    approved+summarized video, full stop -- a superset of
    needs_video_summary_refresh, no staleness comparison at all. Added
    once products.description started getting backfilled onto already-
    scraped products (see parse_description in the four *_product_
    scraper modules): a description change doesn't move the approved+
    summarized video count, so needs_video_summary_refresh's staleness
    check has no way to notice a product's rollup could now say more with
    the new context available -- by that filter's definition, a rollup
    already built from the right number of videos IS current. This filter
    is the deliberately-blunt "just regenerate everything" escape hatch
    for that case (or any other "the inputs changed in a way the count-
    based check can't see" situation), meant for an occasional one-time
    catalog-wide pass (see scripts/backfill_video_review_rollups.py's
    REFRESH_ALL), not routine/scheduled use the way needs_video_summary_
    refresh is.

    missing_core=True: products with no core_id set yet (migration 007).
    Built for scripts/backfill_core_ids.py -- see that script and
    queue_rescrape() below. Every product scraped before the cores table
    was wired up matches this, plus any product whose page genuinely
    doesn't expose a parseable core name.

    p.release_date: Al asked directly whether MOTIV's on-page "available"
    date could show up as a release date column on Products -- turned out
    every scraper (product_scraper/commercebuild/woocommerce/netsuite) was
    already parsing and persisting this via each platform's own
    parse_release_date + upsert_product's coalesce-preserve-existing
    pattern (see 003_date_tracking_and_bowwwl.sql), it just wasn't
    selected here or rendered anywhere -- this curated column list is
    hand-picked, not select *, unlike get_product's p.* (see that
    function's own docstring). Added here so it's actually visible, not a
    new data source. announced_date is the harder, separate ask Al flagged
    himself (a real, different manufacturer-published concept -- see
    003's own comment on that reserved column) -- no platform exposes a
    distinct "announced" date separate from release/availability
    anywhere in its HTML, especially not for older/historic balls, so it
    stays unpopulated and out of this column list until a real source
    turns up.

    missing_coverstock=True: products with no coverstock_id set yet
    (migration 008) -- Al's direct follow-up to the cores work, "can we
    do the same thing we did for cores for covers, those are also shared
    across many balls". Same data-quality-visibility purpose as
    missing_core, though the underlying gap is smaller here: unlike
    core_id (which needed a live rescrape of every product to backfill,
    since family_id/core_id was never populated before migration 007),
    coverstock_id was backfilled for every already-populated
    coverstock_name in migration 008 itself -- so this filter should only
    ever catch products whose page genuinely never exposed a parseable
    coverstock, not a backlog waiting on a rescrape. p.coverstock_id/
    p.coverstock_name are also now selected (the latter already existed
    as a real per-product column, see migration 008's own comment on why
    it wasn't dropped) so the Products tab can show and link into a
    shared coverstock without a second lookup.

    missing_skus=True: products with ZERO product_skus rows -- unlike
    missing_core/missing_coverstock (a nullable column directly on
    products), product_skus is a separate table, so this is a `not
    exists` subquery rather than an `is null` check. Built for
    scripts/rescrape_commercebuild_products.py after a real incident
    (Al: product 56897c0b-e3ec-4314-a8dc-238e1b8b7a75, Storm Tropical
    Surge Black/Cherry, had zero product_skus despite its real page
    clearly showing weight/RG/differential values -- root cause was
    commercebuild_product_scraper's parse_tech_data_pdf_url missing a
    "Tech Sheet" wording variant, now fixed, see that module's docstring)
    -- this filter is how to find every OTHER product that fell into the
    same silent gap before the fix shipped, regardless of platform (a
    scrape/parse failure that produces zero SKUs isn't unique to
    commercebuild, even though that's the one confirmed real case so
    far).

    html_fallback_skus=True: products whose product_skus rows all have
    source='html' (and have at least one such row) -- deliberately
    distinct from missing_skus (zero rows) above, this is the OTHER
    real gap that fix left: 900 Global's image-based Tech Data PDFs
    (Al: "viking still only has 1 sku") get exactly one source='html'
    row from commercebuild_product_scraper's _html_fallback_skus
    stopgap, which means they now have a nonzero product_skus count and
    stop showing up under missing_skus at all -- this filter is how to
    find them again now that parse_tech_data_pdf_via_textract exists to
    actually recover the full table on a rescrape. `exists (...)` +
    `not exists (... source <> 'html')` together (not just the `not
    exists` alone) so a genuinely zero-row product doesn't vacuously
    match here too -- that's missing_skus' job, not this one's. A
    product that already got its full PDF-sourced table (source='pdf')
    correctly never matches, even if it also happens to have a stray
    source='html' row from some earlier partial state, since ANY
    non-html row disqualifies it.

    REAL BUG, caught live via the batch panel itself, Al pasted a batch
    log of 1034 matched products that turned out to be almost entirely
    real Hammer/Track/Ebonite ball names (Black Widow, Raw Hammer,
    Theorem Delta, Paradox, Scandal, etc -- confirmed against this
    repo's own tests/fixtures/hammer_*.json/track_*.json). Root cause:
    every OTHER platform's scraper (product_scraper/Brunswick,
    woocommerce_product_scraper/SWAG, netsuite_product_scraper/MOTIV,
    shopify_product_scraper/Hammer+Track+Ebonite) writes source='html'
    as its SKUs' ONLY, NORMAL, CORRECT source -- those platforms get
    their real per-weight RG/Diff data straight from HTML, no PDF
    involved at all, so "every row is source='html'" is the expected
    healthy state there, not a sign of anything broken. Only
    commercebuild's own scraper ever writes source='pdf' as its normal
    path AND falls back to a single source='html' stopgap row -- so only
    on THAT platform does "every row is html" actually mean "still
    needs the real table." Scoped to `p.source_platform = 'commercebuild'`
    below to fix this -- without that scope this filter fired a pointless
    rescrape against roughly the entire non-commercebuild catalog.

    missing_video_candidates=True: products with ZERO product_videos rows
    of ANY status -- not just "no approved summary" the way has_approved_
    video_summaries/needs_video_summary_refresh check. Al's direct ask
    after learning VideoDiscoveryFunction's actual search job (the thing
    that calls YouTube's search.list to find candidate review videos in
    the first place) is deliberately manual/invoke-only, not scheduled,
    because search.list is capped at a hard 100 calls/day for this
    project -- there's no automatic "search every new product" step. This
    filter is how to find every product that has never had a video search
    run against it at all (a genuinely-searched-but-came-up-empty product
    would still have zero product_videos rows too, and is indistinguishable
    from a never-searched one here -- see video_discovery/app.py's own
    fetch_products_to_search rotation logic and last_video_discovery_at
    column, migration 005, for a per-product "when was this last
    searched" signal this filter deliberately doesn't need/use, since the
    ask here was just "which products have nothing at all yet", not "which
    are overdue for a re-search"). Same `not exists` shape as missing_skus
    (product_videos is a separate table, not a nullable column on
    products).

    popularity_score, total_daily_movement, demand_score: as of migration
    030 (see that migration's own header comment for the real performance
    incident that prompted it), these are plain columns on `products`,
    recomputed once a day by refresh_product_scores -- NOT computed here
    anymore. Before 030 this query ran _POPULARITY_SCORE_SQL/_TOTAL_
    DAILY_MOVEMENT_SQL/_DEMAND_SCORE_CTE (correlated subqueries + a
    whole-catalog percent_rank() CTE) on every single call; that code is
    gone from this function entirely, not just unused, since
    refresh_product_scores/app.py is now the one and only place those
    formulas live. Always selected unconditionally (same "cheap enough to
    include without a separate round-trip" reasoning as before -- more
    true than ever now that it's a plain column read, not a computed
    subquery). Accepted sort values (see _SORT_ORDER_BY above):
    'popularity'/'total_daily_movement'/'demand_score' (each desc),
    'newest'/'oldest' (release_date), 'name_asc'/'name_desc'
    (alphabetical). Anything else (including the default None) keeps the
    existing updated_at-desc order, same unrecognized-value-is-harmless
    convention as every other filter/sort value on this endpoint.

    article_id/article_status: left-joined from product_articles (022_
    product_articles.sql, one row per product, unique on product_id).
    article_status is null when no row exists yet (never generated) --
    admin-spa's Products tab uses that three-way null/pending/approved
    (rejected also passes through as-is) split to render a status icon
    per row without a second round-trip per product. Al's direct ask:
    "add icons to the product list items to click on the different
    elements that could be associated with them... an article icon with
    state so green if approved, yellow if pending, and grey if not
    generated." article_id rides along so the icon can link straight to
    that product's Article sub-tab without admin-spa needing to guess or
    re-fetch it.

    video_count/approved_video_count/approved_summarized_video_count:
    the same idea extended to product_videos, Al's direct follow-up:
    "can we add one similar for videos. similar state. grey if none
    approved and yellow if approve but no summaries and green if
    approved and summaries. maybe a count next to the icon for number
    of videos." Unlike product_articles this is a genuine one-to-many
    table (many candidate videos per product), so a plain left join
    would fan out the row -- `left join lateral (...) v on true`
    computes all three counts in one pass over that product's own
    product_videos rows instead (Postgres runs the subquery once per
    outer row, correlated on p.id, same as a scalar subquery would, but
    without needing three separate subqueries/round-trips through the
    table for video_count vs approved_video_count vs approved_
    summarized_video_count). video_count is every row regardless of
    status (what "number of videos" literally asked for); the other two
    are what admin-spa derives the icon's grey/yellow/green state from
    -- approved_video_count = 0 is grey (nothing approved yet, whether
    that's because none exist or none have been reviewed), > 0 with
    approved_summarized_video_count still 0 is yellow (approved but
    video_summarizer hasn't produced a per-video summary for any of
    them yet), and approved_summarized_video_count > 0 is green. All
    three coalesce to 0 (not null) for a product with zero product_
    videos rows at all, via `coalesce(v.video_count, 0)` etc., so
    admin-spa never has to null-check these the way it does for
    article_status."""
    # p alias + left join cores: needed once c.name entered the picture --
    # products and cores both have a plain "name" column, so every
    # previously-bare column reference below (name, published, brand_id,
    # updated_at, id) got a p. prefix to stay unambiguous, even though
    # none of them actually change meaning.
    # join brands too (b.name as brand_name) -- the Products tab used to
    # show a truncated brand_id UUID in its list column, not useful for
    # actually recognizing a brand at a glance. Added alongside the new
    # brand filter dropdown (see list_brands below) so filtering by brand
    # and reading which brand a row belongs to both work off a real name,
    # not a UUID you'd have to look up separately.
    # left join product_articles too (pa.id/pa.status) -- one row max per
    # product (unique on product_id), so this join can never fan out the
    # result set the way a videos/skus join would.
    # left join lateral product_videos aggregates -- see video_count's own
    # docstring paragraph above for why this needs to be a lateral
    # subquery (one-to-many) rather than a plain join the way product_
    # articles' one-to-one join above works.
    query = f"""
        select p.id, p.brand_id, b.name as brand_name, p.name, p.url, p.status, p.published, p.updated_at,
               p.core_id, c.name as core_name, p.release_date, p.coverstock_id, p.coverstock_name,
               p.popularity_score, p.total_daily_movement, p.demand_score,
               pa.id as article_id, pa.status as article_status,
               coalesce(v.video_count, 0) as video_count,
               coalesce(v.approved_video_count, 0) as approved_video_count,
               coalesce(v.approved_summarized_video_count, 0) as approved_summarized_video_count
        from products p
        left join cores c on c.id = p.core_id
        left join brands b on b.id = p.brand_id
        left join product_articles pa on pa.product_id = p.id
        left join lateral (
            select
                count(*) as video_count,
                count(*) filter (where pv.status = 'approved') as approved_video_count,
                count(*) filter (where pv.status = 'approved' and pv.summary is not null) as approved_summarized_video_count
            from product_videos pv
            where pv.product_id = p.id
        ) v on true
        where 1=1
    """
    params = []
    if published is not None:
        query += " and p.published = %s"
        params.append(published)
    if brand_id:
        query += " and p.brand_id = %s"
        params.append(brand_id)
    if search:
        query += " and p.name ilike %s"
        params.append(f"%{search}%")
    if needs_video_summary_refresh:
        query += """
            and exists (
                select 1 from product_videos pv
                where pv.product_id = p.id and pv.status = 'approved' and pv.summary is not null
            )
            and (
                p.video_reviews_summary is null
                or p.video_reviews_summary_video_count <> (
                    select count(*) from product_videos pv2
                    where pv2.product_id = p.id and pv2.status = 'approved' and pv2.summary is not null
                )
            )
        """
    if has_approved_video_summaries:
        query += """
            and exists (
                select 1 from product_videos pv
                where pv.product_id = p.id and pv.status = 'approved' and pv.summary is not null
            )
        """
    if missing_core:
        query += " and p.core_id is null"
    if missing_coverstock:
        query += " and p.coverstock_id is null"
    if missing_skus:
        query += " and not exists (select 1 from product_skus ps where ps.product_id = p.id)"
    if html_fallback_skus:
        query += """
            and p.source_platform = 'commercebuild'
            and exists (select 1 from product_skus ps3 where ps3.product_id = p.id)
            and not exists (select 1 from product_skus ps4 where ps4.product_id = p.id and ps4.source <> 'html')
        """
    if missing_video_candidates:
        query += " and not exists (select 1 from product_videos pv where pv.product_id = p.id)"
    if source_platform:
        query += " and p.source_platform = %s"
        params.append(source_platform)
    if status:
        query += " and p.status = %s"
        params.append(status)
    # id as a final tiebreaker -- same reason list_video_candidates and
    # fetch_products_to_search needed one (see admin_api/service.py's own
    # earlier fix and video_discovery/app.py's ROTATION section): rows
    # sharing an updated_at value (or, now, a popularity_score value) make
    # plain OFFSET/LIMIT pagination unstable, and this endpoint is now
    # paginated by a real consumer (the backfill script) as of this
    # filter's addition.
    query += " order by " + _SORT_ORDER_BY.get(sort, _DEFAULT_ORDER_BY) + " limit %s offset %s"
    params += [limit, offset]

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def list_sku_weights(conn) -> list:
    """Distinct product_skus.weight_lbs values across the WHOLE catalog
    (not just SKUs with a computable daily movement/days-of-supply), sorted ascending
    -- backs the Dashboard's Days-of-Supply weight-toggle filter. Al: "can
    we put a filter so we can toggle the different weights so that we can
    see 15 only or 15 and 14 etc," same 'toggle a few states' UI
    convention list_products' own filter dropdowns already established.
    Deliberately whole-catalog, not scoped to today's top-10-worst-DOS
    SKUs -- a weight that isn't in today's top 10 should still be an
    available checkbox (its SKUs just don't happen to be the most urgent
    right now), not silently missing from the toggle."""
    with conn.cursor() as cur:
        cur.execute("select distinct weight_lbs from product_skus where weight_lbs is not null order by weight_lbs")
        return [row[0] for row in cur.fetchall()]


def get_top_days_of_supply(conn, weight_lbs: list = None) -> list:
    """Up to 10 {product_id, name, brand_name, weight_lbs, daily_movement,
    latest_quantity, days_of_supply} dicts, ascending by days_of_supply
    (soonest to stock out first) -- Al: "top 10 days of supply skus
    descending so lowest number of days first." PER-SKU (not per-product
    like top_popularity/top_daily_movement), since days-of-supply is
    meaningless summed across a product's weights -- a 16lb about to stock
    out doesn't average out with a 12lb that's overstocked.
    daily_movement > 0 is required (daily_movement <= 0 -> an infinite or
    negative days-of-supply, not a real answer, same reasoning
    top_daily_movement's own > 0 filter uses); latest_quantity is that
    SKU's single most recent quantity reading (not windowed -- "right
    now", same as computeSkuForecast's own latestQuantity parameter), so a
    quantity of 0 correctly sorts to days_of_supply = 0 at the very top --
    already-out-of-stock is the most urgent case, exactly matching
    computeSkuForecast's own `latestQuantity <= 0 -> daysOfSupply: 0`
    branch (admin-site/index.html) rather than a divide-by-zero or an
    excluded row.

    Split out of get_dashboard_summary into its own function/endpoint
    (GET /admin/dashboard/days-of-supply, same reasoning
    get_catalog_daily_movement_history already established for a
    Dashboard piece that needs independent refresh) specifically so the
    weight_lbs filter below doesn't force re-running the other six
    dashboard queries every time a checkbox is toggled --
    get_dashboard_summary no longer includes this list at all; the
    Dashboard tab fetches it as a third parallel call alongside GET
    /admin/dashboard and GET /admin/catalog-daily-movement-history.

    weight_lbs (optional): list of ints, e.g. [15, 14] -- Al: "so we can
    see 15 only or 15 and 14 etc." Rendered as `and sk.weight_lbs =
    any(%s)`, same positional-bound-array-param convention
    list_price_sources_for_products' own `product_id = any(%s::uuid[])`
    filter already uses (psycopg2 adapts a Python list straight to a
    Postgres array). None/empty means no filter -- every weight, exactly
    today's original unfiltered behavior."""
    weight_filter_sql = "and sk.weight_lbs = any(%s)" if weight_lbs else ""
    params = [weight_lbs] if weight_lbs else []
    with conn.cursor() as cur:
        cur.execute(f"""
            with sa as {_sku_daily_movement_cte(DAILY_MOVEMENT_LOOKBACK_DAYS)},
            latest_qty as (
                select distinct on (product_sku_id) product_sku_id, quantity
                from product_sku_stock_history
                where quantity is not null
                order by product_sku_id, checked_at desc
            )
            select p.id as product_id, p.name, b.name as brand_name, sk.weight_lbs,
                   round(sa.daily_movement::numeric, 2) as daily_movement,
                   lq.quantity as latest_quantity,
                   round((lq.quantity / sa.daily_movement)::numeric, 1) as days_of_supply
            from sa
            join product_skus sk on sk.id = sa.product_sku_id
            join products p on p.id = sk.product_id
            left join brands b on b.id = p.brand_id
            join latest_qty lq on lq.product_sku_id = sa.product_sku_id
            where sa.daily_movement > 0 and lq.quantity is not null
            {weight_filter_sql}
            order by (lq.quantity / sa.daily_movement) asc, sk.id asc
            limit 10
        """, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_dashboard_summary(conn) -> dict:
    """Real ask from Al: "can we create an admin dashboard with some KPIs
    and top 10 lists... Top 10s i think we can do Popularity and ADUs. An
    interesting number would be total ADUs across all balls, ADUs by
    brand, and things like that" (that quote predates the 2026-09-06
    ADU->Daily Movement rename -- the underlying metric is unchanged,
    only the name). Backs a new Dashboard tab -- one endpoint, not
    several, since every number here is read-only and the tab renders
    them all at once on load (same "one round trip" reasoning
    list_products' popularity_score/total_daily_movement columns already
    used at this catalog's size).

    Reads the same materialized popularity_score/total_daily_movement
    columns (030_materialized_product_scores.sql) that list_products'
    own columns and sort options already read, rather than re-deriving
    either formula a second time -- both are recomputed daily by
    refresh_product_scores/app.py, the single source of truth for these
    formulas now, so the Dashboard's numbers are guaranteed to agree with
    what the Products tab already shows for the same product, not a
    second, potentially-drifting definition of "popularity" or "daily
    movement".

    Six separate queries, not one giant one: KPI counts, top 10
    popularity, top 10 daily movement, daily-movement-by-brand, top 10
    growing daily movement, and top 10 shrinking daily movement are
    structurally unrelated result shapes (one row vs ten rows vs one row
    per brand vs ten per-SKU rows) that don't share a natural GROUP BY, so
    combining them would mean either extra round trips anyway (subqueries
    returning arrays) or a much harder to read query for no real
    performance win at this catalog's size.

    NOTE: top_days_of_supply is deliberately NOT included here -- see
    get_top_days_of_supply's own docstring for why it was split into its
    own function/endpoint (GET /admin/dashboard/days-of-supply) instead of
    being a seventh query below.

    Returns:
      kpis: single dict -- total_products, current_products,
        retired_products, missing_core, missing_coverstock, missing_skus,
        products_with_video (>=1 approved+summarized video),
        products_with_price_tracking (>=1 approved+active price source),
        total_catalog_daily_movement (sum of every product's
        total_daily_movement).
      top_popularity: up to 10 {id, name, brand_name, popularity_score}
        dicts, popularity_score > 0 only (a product with zero approved
        videos has nothing meaningful to rank -- omitted rather than
        padding the list with ties at 0).
      top_daily_movement: up to 10 {id, name, brand_name,
        total_daily_movement} dicts, same > 0 reasoning (a product with
        no qualifying SKU stock readings contributes nothing meaningful
        to a "top movers" list).
      daily_movement_by_brand: one {brand_name, total_daily_movement}
        dict per brand (every brand, even one with total_daily_movement
        = 0 -- Al's own "ADUs by brand" ask (see this function's own
        opening quote) reads as "show me the breakdown", which is more
        useful as a complete picture across the whole catalog than a
        filtered list), ordered highest total_daily_movement first.
      top_growing_daily_movement / top_shrinking_daily_movement: up to 10
        {product_id, name, brand_name, weight_lbs,
        previous_daily_movement, current_daily_movement,
        delta_daily_movement} dicts each -- Al's two-sided ask, mirror-
        image queries (same shape as top_popularity/top_daily_movement
        being separate despite their own near-identical shape).
        delta_daily_movement = current_daily_movement -
        previous_daily_movement, comparing this SKU's current
        DAILY_MOVEMENT_LOOKBACK_DAYS-day rate against its own rate over
        the DAILY_MOVEMENT_LOOKBACK_DAYS days before that (see
        _sku_daily_movement_cte's own docstring for the two-window
        definition). top_growing_daily_movement keeps
        delta_daily_movement > 0 ordered desc (biggest increase first);
        top_shrinking_daily_movement keeps delta_daily_movement < 0
        ordered asc (biggest decrease first). HONEST CAVEAT, same
        "disclosed not glossed over" convention as
        get_catalog_daily_movement_history's own docstring: both lists
        require a SKU to have a qualifying rate in BOTH windows, i.e.
        genuinely need up to 2*DAILY_MOVEMENT_LOOKBACK_DAYS (60) days of
        accumulated product_sku_stock_history to ever return anything --
        expect these two lists to stay empty for a while on a freshly-
        launched catalog (see admin-site's own empty-state copy for
        this).
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            select
                (select count(*) from products) as total_products,
                (select count(*) from products where status = 'current') as current_products,
                (select count(*) from products where status = 'retired') as retired_products,
                (select count(*) from products where core_id is null) as missing_core,
                (select count(*) from products where coverstock_id is null) as missing_coverstock,
                (select count(*) from products p where not exists (
                    select 1 from product_skus ps where ps.product_id = p.id
                )) as missing_skus,
                (select count(distinct pv.product_id) from product_videos pv
                    where pv.status = 'approved' and pv.summary is not null) as products_with_video,
                (select count(distinct pps.product_id) from product_price_sources pps
                    where pps.status = 'approved' and pps.is_active) as products_with_price_tracking,
                (select coalesce(sum(total_daily_movement), 0) from products) as total_catalog_daily_movement
        """)
        columns = [desc[0] for desc in cur.description]
        kpis = dict(zip(columns, cur.fetchone()))

        # popularity_score/total_daily_movement below: plain reads of the
        # materialized products columns (030_materialized_product_scores.
        # sql) -- these two queries used to each recompute the same
        # correlated-subquery formula list_products' own popularity_score/
        # total_daily_movement columns already used, over the WHOLE
        # catalog, a THIRD and FOURTH time on top of what a Dashboard
        # page load's other queries already cost. See this file's own
        # comment above DAILY_MOVEMENT_LOOKBACK_DAYS for the full incident
        # writeup.
        cur.execute("""
            select p.id, p.name, b.name as brand_name, p.popularity_score
            from products p
            left join brands b on b.id = p.brand_id
            where p.popularity_score > 0
            order by p.popularity_score desc, p.id asc
            limit 10
        """)
        columns = [desc[0] for desc in cur.description]
        top_popularity = [dict(zip(columns, row)) for row in cur.fetchall()]

        cur.execute("""
            select p.id, p.name, b.name as brand_name, p.total_daily_movement
            from products p
            left join brands b on b.id = p.brand_id
            where p.total_daily_movement > 0
            order by p.total_daily_movement desc, p.id asc
            limit 10
        """)
        columns = [desc[0] for desc in cur.description]
        top_daily_movement = [dict(zip(columns, row)) for row in cur.fetchall()]

        # Daily movement by brand: a real GROUP BY aggregate (not a read
        # of the materialized total_daily_movement column like the query
        # above) -- reimplements the same per-SKU daily-movement
        # definition (drops-only, DAILY_MOVEMENT_LOOKBACK_DAYS window,
        # >=2 readings required), but un-correlated from any single
        # product so it can be grouped by brand directly instead of
        # running once per product and summing in Python. MUST stay in
        # lockstep with refresh_product_scores/app.py's own copy of this
        # same formula, same hand-synced-constant reasoning documented
        # above DAILY_MOVEMENT_LOOKBACK_DAYS.
        cur.execute(f"""
            with sku_daily_movement as (
                select h.product_sku_id,
                       sum(case when h.delta < 0 then -h.delta else 0 end) as units_sold,
                       extract(epoch from (max(h.checked_at) - min(h.checked_at))) / 86400.0 as elapsed_days
                from (
                    select psh.product_sku_id, psh.checked_at,
                           psh.quantity - lag(psh.quantity) over (partition by psh.product_sku_id order by psh.checked_at) as delta
                    from product_sku_stock_history psh
                    where psh.quantity is not null
                      and psh.checked_at >= now() - interval '{DAILY_MOVEMENT_LOOKBACK_DAYS} days'
                ) h
                group by h.product_sku_id
                having count(*) >= 2
            ),
            product_daily_movement as (
                select ps.product_id,
                       sum(case when sa.elapsed_days > 0 then sa.units_sold / sa.elapsed_days else 0 end) as total_daily_movement
                from product_skus ps
                join sku_daily_movement sa on sa.product_sku_id = ps.id
                group by ps.product_id
            )
            select b.name as brand_name, coalesce(sum(pa.total_daily_movement), 0) as total_daily_movement
            from brands b
            left join products p on p.brand_id = b.id
            left join product_daily_movement pa on pa.product_id = p.id
            group by b.name
            order by total_daily_movement desc
        """)
        columns = [desc[0] for desc in cur.description]
        daily_movement_by_brand = [dict(zip(columns, row)) for row in cur.fetchall()]

        # Growing daily movement, biggest increase first (Al: "top 10
        # growth ADUs... by sku" -- that quote predates the 2026-09-06
        # ADU->Daily Movement rename, same metric) -- current
        # DAILY_MOVEMENT_LOOKBACK_DAYS-day window vs the
        # DAILY_MOVEMENT_LOOKBACK_DAYS days before that, see
        # _sku_daily_movement_cte's own docstring for the two-window
        # definition and this function's own docstring for the "needs up
        # to 60 days of history" caveat.
        cur.execute(f"""
            with cw as {_sku_daily_movement_cte(DAILY_MOVEMENT_LOOKBACK_DAYS)},
            pw as {_sku_daily_movement_cte(2 * DAILY_MOVEMENT_LOOKBACK_DAYS, DAILY_MOVEMENT_LOOKBACK_DAYS)}
            select p.id as product_id, p.name, b.name as brand_name, sk.weight_lbs,
                   round(pw.daily_movement::numeric, 2) as previous_daily_movement,
                   round(cw.daily_movement::numeric, 2) as current_daily_movement,
                   round((cw.daily_movement - pw.daily_movement)::numeric, 2) as delta_daily_movement
            from cw
            join pw on pw.product_sku_id = cw.product_sku_id
            join product_skus sk on sk.id = cw.product_sku_id
            join products p on p.id = sk.product_id
            left join brands b on b.id = p.brand_id
            where cw.daily_movement is not null and pw.daily_movement is not null and (cw.daily_movement - pw.daily_movement) > 0
            order by (cw.daily_movement - pw.daily_movement) desc, sk.id asc
            limit 10
        """)
        columns = [desc[0] for desc in cur.description]
        top_growing_daily_movement = [dict(zip(columns, row)) for row in cur.fetchall()]

        # Shrinking daily movement -- mirror image of
        # top_growing_daily_movement immediately above (same
        # shape/columns, opposite filter+sort direction), same "separate
        # query despite near-identical shape" convention
        # top_popularity/top_daily_movement already established.
        cur.execute(f"""
            with cw as {_sku_daily_movement_cte(DAILY_MOVEMENT_LOOKBACK_DAYS)},
            pw as {_sku_daily_movement_cte(2 * DAILY_MOVEMENT_LOOKBACK_DAYS, DAILY_MOVEMENT_LOOKBACK_DAYS)}
            select p.id as product_id, p.name, b.name as brand_name, sk.weight_lbs,
                   round(pw.daily_movement::numeric, 2) as previous_daily_movement,
                   round(cw.daily_movement::numeric, 2) as current_daily_movement,
                   round((cw.daily_movement - pw.daily_movement)::numeric, 2) as delta_daily_movement
            from cw
            join pw on pw.product_sku_id = cw.product_sku_id
            join product_skus sk on sk.id = cw.product_sku_id
            join products p on p.id = sk.product_id
            left join brands b on b.id = p.brand_id
            where cw.daily_movement is not null and pw.daily_movement is not null and (cw.daily_movement - pw.daily_movement) < 0
            order by (cw.daily_movement - pw.daily_movement) asc, sk.id asc
            limit 10
        """)
        columns = [desc[0] for desc in cur.description]
        top_shrinking_daily_movement = [dict(zip(columns, row)) for row in cur.fetchall()]

    return {
        "kpis": kpis,
        "top_popularity": top_popularity,
        "top_daily_movement": top_daily_movement,
        "daily_movement_by_brand": daily_movement_by_brand,
        "top_growing_daily_movement": top_growing_daily_movement,
        "top_shrinking_daily_movement": top_shrinking_daily_movement,
    }


def get_catalog_daily_movement_history(conn) -> list:
    """Real follow-up ask, same session, Al: "can we add some data over
    time charts to the dashboard, maybe total catalog adu over time
    similar to what we have per product 7d, 30d, 90d, 1y and all
    picker" (that quote predates the 2026-09-06 ADU->Daily Movement
    rename -- same underlying metric, name only changed). Backs a new line chart on the
    Dashboard tab, reusing the SAME client-side range-picker machinery
    (CHART_RANGE_PRESETS/filterHistoryByRange/buildChartRangeToolbar in
    admin-site/index.html) the price/SKU-stock charts already
    established: fetch the FULL history once, filter to 7D/30D/90D/1Y/All
    client-side, so switching ranges is instant with no re-fetch -- same
    reasoning as loadProductDetailInto's own days:3650 comment.

    IMPORTANT, disclosed rather than silently glossed over: this is a
    REAL but DIFFERENTLY-DEFINED number from the Dashboard's own
    `kpis.total_catalog_daily_movement` (see get_dashboard_summary
    above), not a time-series of that exact same point-in-time formula.
    The KPI card's total_catalog_daily_movement is products.
    total_daily_movement (030) summed across every product -- a PER-SKU
    trailing-DAILY_MOVEMENT_LOOKBACK_DAYS-window figure that only counts
    a SKU at all once it has >=2 readings in that specific window (see
    refresh_product_scores/app.py's docstring, point 2). Re-running that exact
    per-SKU-gated definition at every historical calendar day would need
    one correlated subquery PER DAY in the requested range -- expensive,
    and arguably not even what "daily movement over time" should look
    like day to day (a SKU dropping in and out of "has >=2 readings this
    window" would make the line jump around for reasons that have
    nothing to do with real demand). Instead: total units sold (drops
    only, restocks excluded -- same interpretation as everywhere else in
    this project) across EVERY SKU in the whole catalog, bucketed by
    calendar day, then a rolling DAILY_MOVEMENT_LOOKBACK_DAYS-day
    trailing SUM divided by DAILY_MOVEMENT_LOOKBACK_DAYS (a flat 30, not
    each SKU's own actual elapsed-reading-span) for every day in the
    observed history -- a real, honest, catalog-wide rolling average,
    just not byte-identical to the KPI card's own snapshot. In practice
    the two should track each other closely and the chart's rightmost
    point will usually be close to (but is not guaranteed to exactly
    equal) the KPI card's current total_catalog_daily_movement -- worth
    knowing if Al or a future dev ever compares the two and expects them
    to match exactly.

    Days with zero recorded drops are real zeros, not gaps -- the
    generate_series call below fills in every calendar day across the
    full observed product_sku_stock_history range (not just days that
    happen to have a reading), so the rolling window's divisor is always
    a true 30 calendar days, never silently shrunk by missing days the
    way a naive `group by day` alone would drop entirely."""
    with conn.cursor() as cur:
        cur.execute(f"""
            with bounds as (
                select min(checked_at)::date as min_day, max(checked_at)::date as max_day
                from product_sku_stock_history
                where quantity is not null
            ),
            days as (
                select generate_series(min_day, max_day, interval '1 day')::date as day
                from bounds
                where min_day is not null
            ),
            all_deltas as (
                select psh.product_sku_id, psh.checked_at::date as day,
                       psh.quantity - lag(psh.quantity) over (partition by psh.product_sku_id order by psh.checked_at) as delta
                from product_sku_stock_history psh
                where psh.quantity is not null
            ),
            daily_units_sold as (
                select day, sum(case when delta < 0 then -delta else 0 end) as units_sold
                from all_deltas
                group by day
            )
            select d.day,
                   coalesce(sum(dus.units_sold) over (
                       order by d.day rows between {DAILY_MOVEMENT_LOOKBACK_DAYS - 1} preceding and current row
                   ), 0) / {float(DAILY_MOVEMENT_LOOKBACK_DAYS)} as total_daily_movement
            from days d
            left join daily_units_sold dus on dus.day = d.day
            order by d.day
        """)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_product(conn, product_id: str):
    """Real ask from Al: he's noticed data issues in the admin UI he
    suspects trace back to the scrapers, and wants every column visible
    (not just the curated subset each field previously hand-picked) so
    gaps become visible by inspection rather than by guessing which
    column might be the problem. Two changes from the previous version:
    p.* was already everything on products itself, but product_skus and
    product_images were each trimmed to a hand-picked column list (missing
    id/product_id/created_at/updated_at/part_number) -- both are now
    `select *`, so nothing on those two child tables is hidden either.

    Also newly surfaced here: discovered_urls (this product's own crawl
    record -- scrape_status/sitemap_lastmod/last_scraped_at, matched by
    url since that table has no product_id FK, only brand_id+url; null
    if this product was never discovered through the normal sitemap/
    collection crawl, e.g. inserted by hand like the Hammerhead product
    from an earlier session) and bowlerdepot_matches/bowwwl_matches (this
    product's reconciliation rows against BowlerDepot's BigCommerce
    catalog and bowwwl.com -- both real tables that migrations 001/003
    created but that NO admin_api endpoint has ever exposed before this).
    brand_name/manufacturer_name are also joined on for readability --
    p.brand_id alone is a bare UUID, not something you can eyeball for a
    data-quality pass.

    product_videos is deliberately NOT duplicated here even though it's
    real per-product data too -- GET /video-candidates?product_id=...
    already exposes it in full (pv.*, see get_video_candidate), and
    duplicating it here would just be two places to keep in sync for no
    discovery benefit.

    Like get_core/get_review_item, this real multi-join/multi-query
    function isn't unit tested against its actual SQL text beyond the
    not-found short-circuit and a hand-built sequenced-fake covering the
    assembly logic (see test_get_product_assembles_all_related_data) --
    no real Postgres in this sandbox to exercise the joins themselves
    against."""
    with conn.cursor() as cur:
        # Left join cores (migration 007), brands, and manufacturers so
        # the detail view can show human-readable names alongside the
        # bare id columns p.* already carries (core_id, brand_id).
        cur.execute(
            """
            select p.*, c.name as core_name, c.core_type as core_type,
                   b.name as brand_name, m.name as manufacturer_name
            from products p
            left join cores c on c.id = p.core_id
            left join brands b on b.id = p.brand_id
            left join manufacturers m on m.id = b.manufacturer_id
            where p.id = %s
            """,
            (product_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cur.description]
        product = dict(zip(columns, row))

        cur.execute(
            "select * from product_skus where product_id = %s order by weight_lbs desc",
            (product_id,),
        )
        sku_columns = [desc[0] for desc in cur.description]
        product["skus"] = [dict(zip(sku_columns, row)) for row in cur.fetchall()]

        # Ordered by display_order (migration 010) so the admin-site image
        # grid renders in the admin-curated order rather than whatever
        # order Postgres happens to return rows in -- id as a stable
        # tiebreaker for any row sharing a display_order value (shouldn't
        # normally happen post-migration, but reorder_product_images only
        # writes positions for the ids it's given, so a row untouched by
        # a partial reorder could theoretically collide with one that was).
        cur.execute(
            "select * from product_images where product_id = %s order by display_order, id",
            (product_id,),
        )
        image_columns = [desc[0] for desc in cur.description]
        product["images"] = [dict(zip(image_columns, row)) for row in cur.fetchall()]

        # discovered_urls has no product_id FK (it's brand_id+url, tracking
        # the crawl itself rather than the parsed product) -- matched here
        # by this product's own url, the only real link between the two.
        cur.execute(
            "select * from discovered_urls where url = %s",
            (product["url"],),
        )
        discovered_url_columns = [desc[0] for desc in cur.description]
        discovered_url_row = cur.fetchone()
        product["discovered_url"] = dict(zip(discovered_url_columns, discovered_url_row)) if discovered_url_row else None

        cur.execute(
            "select * from bowlerdepot_products where product_id = %s",
            (product_id,),
        )
        bowlerdepot_columns = [desc[0] for desc in cur.description]
        product["bowlerdepot_matches"] = [dict(zip(bowlerdepot_columns, row)) for row in cur.fetchall()]

        cur.execute(
            "select * from bowwwl_products where product_id = %s",
            (product_id,),
        )
        bowwwl_columns = [desc[0] for desc in cur.description]
        product["bowwwl_matches"] = [dict(zip(bowwwl_columns, row)) for row in cur.fetchall()]

        return product


# ---------------------------------------------------------------------
# Cores (GET /cores, GET /cores/{id}) -- the "other direction" view of the
# same data get_product()/list_products() already surface per-product
# (core_name/core_type joined onto one product row at a time). Product
# is core-agnostic history: this project's whole reason for building the
# cores table in the first place (migration 007) was that multiple named
# products can share one physical core -- Al's example, DV8's Collision
# core used by six differently-named balls -- and that many-to-one shape
# was invisible from the Products tab alone (you'd have to notice six
# different products all showing "Collision" as their core, one page load
# at a time). This surfaces it directly: one row per core, with how many
# products currently reference it, and (on the detail view) exactly which
# ones.
# ---------------------------------------------------------------------

def list_cores(conn, brand_id: str = None, search: str = None, limit: int = 50, offset: int = 0) -> list:
    """product_count comes from a left join + count/group by rather than a
    correlated subquery -- same reasoning as list_products' left join onto
    cores, just the reverse direction: one row per core, zero or more
    matching products rolled up into a single count. A core with zero
    products currently pointing at it (every referencing product got
    reassigned to a different, correctly-named core, or the core was
    created but never actually used by a real product -- e.g. the
    "E "-prefixed rows this exact feature was born out of debugging, see
    DEPLOY_RUNBOOK.md's Hammer incident writeup) still shows up here with
    product_count=0 rather than being silently hidden -- that's a real,
    useful signal (a likely-orphaned row worth cleaning up), not noise to
    filter out by default.

    Ordered by product_count desc, name asc -- cores actually in heavy use
    (the many-products-to-one-core cases this table exists for) surface
    first, ahead of the long tail of single-product or zero-product rows."""
    query = """
        select c.id, c.brand_id, b.name as brand_name, c.name, c.core_type,
               c.release_era, c.created_at, count(p.id) as product_count
        from cores c
        join brands b on b.id = c.brand_id
        left join products p on p.core_id = c.id
        where 1=1
    """
    params = []
    if brand_id:
        query += " and c.brand_id = %s"
        params.append(brand_id)
    if search:
        query += " and c.name ilike %s"
        params.append(f"%{search}%")
    # c.id as a final tiebreaker -- same pagination-stability reasoning as
    # list_products'/list_video_candidates' own id tiebreakers (rows
    # sharing both product_count and name would otherwise paginate
    # unstably).
    query += """
        group by c.id, b.name
        order by product_count desc, c.name asc, c.id asc
        limit %s offset %s
    """
    params += [limit, offset]

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_core(conn, core_id: str):
    """Detail view: the core row itself plus every product currently
    pointing at it (id/name/url/status/published -- enough for the admin
    UI to link straight back into the Products tab's detail view for any
    one of them, same fields that tab's own table already shows). Returns
    None (not an exception) when the id doesn't exist, same convention as
    get_product/get_review_item/get_video_candidate -- app.py maps that to
    a 404."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select c.id, c.brand_id, b.name as brand_name, c.name, c.core_type,
                   c.release_era, c.created_at
            from cores c
            join brands b on b.id = c.brand_id
            where c.id = %s
            """,
            (core_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cur.description]
        core = dict(zip(columns, row))

        cur.execute(
            """
            select id, name, url, status, published, updated_at
            from products
            where core_id = %s
            order by name asc
            """,
            (core_id,),
        )
        product_columns = [desc[0] for desc in cur.description]
        core["products"] = [dict(zip(product_columns, row)) for row in cur.fetchall()]

        return core


# ---------------------------------------------------------------------
# Coverstocks (GET /coverstocks, GET /coverstocks/{id}) -- the exact same
# "other direction" view as Cores above, one migration later (008): a
# coverstock_name is a shared, brand-scoped marketing name multiple
# differently-named products can reuse, invisible from the Products tab
# alone the same way a shared core was before the Cores tab existed. Al's
# own framing when he asked for this: "can we do the same thing we did
# for cores for covers, those are also shared across many balls" --
# confirmed the shared field would be coverstock_name.
# ---------------------------------------------------------------------

def list_coverstocks(conn, brand_id: str = None, search: str = None, limit: int = 50, offset: int = 0) -> list:
    """Same shape as list_cores -- product_count via left join + count/
    group by, ordered by product_count desc so heavily-reused coverstocks
    surface first. A coverstock with zero products currently pointing at
    it (e.g. every referencing product got rescraped under a corrected
    name) still shows up with product_count=0 rather than being hidden --
    same "real, useful signal" reasoning as list_cores' docstring."""
    query = """
        select cs.id, cs.brand_id, b.name as brand_name, cs.name, cs.material, cs.type,
               cs.created_at, count(p.id) as product_count
        from coverstocks cs
        join brands b on b.id = cs.brand_id
        left join products p on p.coverstock_id = cs.id
        where 1=1
    """
    params = []
    if brand_id:
        query += " and cs.brand_id = %s"
        params.append(brand_id)
    if search:
        query += " and cs.name ilike %s"
        params.append(f"%{search}%")
    # cs.id as a final tiebreaker -- same pagination-stability reasoning
    # as list_cores' c.id tiebreaker.
    query += """
        group by cs.id, b.name
        order by product_count desc, cs.name asc, cs.id asc
        limit %s offset %s
    """
    params += [limit, offset]

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_coverstock(conn, coverstock_id: str):
    """Detail view: the coverstock row itself plus every product currently
    pointing at it, same fields/shape as get_core. Returns None (not an
    exception) when the id doesn't exist -- app.py maps that to a 404."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select cs.id, cs.brand_id, b.name as brand_name, cs.name, cs.material, cs.type,
                   cs.created_at
            from coverstocks cs
            join brands b on b.id = cs.brand_id
            where cs.id = %s
            """,
            (coverstock_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cur.description]
        coverstock = dict(zip(columns, row))

        cur.execute(
            """
            select id, name, url, status, published, updated_at
            from products
            where coverstock_id = %s
            order by name asc
            """,
            (coverstock_id,),
        )
        product_columns = [desc[0] for desc in cur.description]
        coverstock["products"] = [dict(zip(product_columns, row)) for row in cur.fetchall()]

        return coverstock


def list_categories(conn) -> list:
    """Learn-site content taxonomy (migration 031) -- Al: "having
    Categories with one being Bowling balls and Ball review being a type
    of article." Small, mostly-static reference data (unlike cores/
    coverstocks, which grow organically out of scraped catalog data), so
    no brand/search filtering or pagination like list_cores/
    list_coverstocks above: just every category, each carrying its own
    article_types nested inline, ordered by display_order. Mirrors what
    public_api's own list_categories returns to the Learn site itself --
    this admin_api copy exists so the Articles review tab can eventually
    show/filter by category+type without a second round-trip design."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, slug, name, description, product_type, display_order
            from categories
            order by display_order, name
            """
        )
        columns = [desc[0] for desc in cur.description]
        categories = [dict(zip(columns, row)) for row in cur.fetchall()]

        cur.execute(
            """
            select id, category_id, slug, name, description, display_order
            from article_types
            order by display_order, name
            """
        )
        at_columns = [desc[0] for desc in cur.description]
        article_types = [dict(zip(at_columns, row)) for row in cur.fetchall()]

    by_category = {}
    for at in article_types:
        by_category.setdefault(at["category_id"], []).append(at)
    for c in categories:
        c["article_types"] = by_category.get(c["id"], [])
    return categories


# ---------------------------------------------------------------------
# Rescrape trigger (POST /products/{id}/rescrape), built for the cores
# backfill (migration 007, scripts/backfill_core_ids.py): core_id only
# gets set the next time a product is actually scraped, and nothing else
# re-triggers that automatically for an already-scraped product. This
# republishes the exact {"url", "brand_id"} job shape every one of the
# four product scrapers already accepts for direct/manual invocation (see
# product_scraper/app.py's _extract_jobs), onto whichever platform's
# scrape queue that product actually belongs to.
# ---------------------------------------------------------------------

# products.source_platform -> the env var holding that platform's
# product-scrape queue URL. craft_cms covers Brunswick/Radical/DV8 (one
# shared queue/scraper -- see template.yaml's RadicalUrlDiscoveryFunction/
# Dv8UrlDiscoveryFunction comments). shopify now covers Hammer (onboarded
# this session -- see src/shopify_url_discovery/app.py) the same way --
# one shared ShopifyProductScrapeQueue/ShopifyProductScraperFunction for
# every brand on the platform, Track/Ebonite included whenever they're
# actually onboarded, since that function's job shape/parsing code is
# brand-agnostic. Any source_platform not listed here still returns None
# from resolve_scrape_queue_env_var, same as before -- not every platform
# has a scraper deployed yet.
SCRAPE_QUEUE_ENV_VAR_BY_PLATFORM = {
    "craft_cms": "PRODUCT_SCRAPE_QUEUE_URL",
    "woocommerce": "WOOCOMMERCE_PRODUCT_SCRAPE_QUEUE_URL",
    "netsuite": "NETSUITE_PRODUCT_SCRAPE_QUEUE_URL",
    "commercebuild": "COMMERCEBUILD_PRODUCT_SCRAPE_QUEUE_URL",
    "shopify": "SHOPIFY_PRODUCT_SCRAPE_QUEUE_URL",
}


def resolve_scrape_queue_env_var(source_platform: str):
    """Pure lookup, no env/DB access -- deliberately returns None rather
    than raising for a platform with no scraper deployed yet, so callers
    can build a graceful "not supported" response instead of a hard
    error. A batch backfill run (scripts/backfill_core_ids.py) shouldn't
    abort just because it reached a product on a platform that isn't
    wired up for rescraping."""
    return SCRAPE_QUEUE_ENV_VAR_BY_PLATFORM.get(source_platform)


def queue_rescrape(conn, product_id: str) -> dict:
    """Looks up the product's url/brand_id/source_platform and publishes
    a fresh scrape job for it. Returns {"queued": True, "product_id",
    "url", "queue_env_var"} on success, or {"queued": False, "reason"}
    (not an exception) when the product's platform has no scraper
    deployed yet or that platform's queue URL isn't configured on this
    stack -- both are expected, non-error states a batch caller should
    log and move past, not treat as a failure worth stopping for.

    Raises LookupError if the product_id itself doesn't exist -- that one
    IS a caller error, same as set_product_published above."""
    with conn.cursor() as cur:
        cur.execute(
            "select url, brand_id, source_platform from products where id = %s",
            (product_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product with id {product_id}")
        url, brand_id, source_platform = row

    env_var = resolve_scrape_queue_env_var(source_platform)
    if env_var is None:
        return {"queued": False, "reason": f"no scraper deployed for source_platform={source_platform!r} yet"}

    queue_url = os.environ.get(env_var)
    if not queue_url:
        return {"queued": False, "reason": f"{env_var} is not configured on this deployment"}

    import boto3

    sqs = boto3.client("sqs")
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({"url": url, "brand_id": str(brand_id)}),
    )
    return {"queued": True, "product_id": product_id, "url": url, "queue_env_var": env_var}


def queue_video_discovery(conn, product_id: str) -> dict:
    """On-demand "search for videos again" trigger (POST
    /products/{id}/discover-videos), built for the product detail view's
    new Videos section -- Al: "if we could add a button with them to
    search for candidates again." VideoDiscoveryFunction already accepts a
    {"product_ids": [...]} scope for exactly this (see its own module
    docstring's job-shape list); this is just the first thing in this
    project to actually invoke it from admin_api rather than by hand via
    `aws lambda invoke`.

    Unlike queue_rescrape (which publishes to an SQS queue a scraper
    Lambda is already subscribed to), there's no queue in front of
    VideoDiscoveryFunction to publish onto -- it's invoke-only, so this
    calls lambda:InvokeFunction directly with InvocationType='Event'
    (async/fire-and-forget). Deliberately async: a real single-product
    search.list call plus DB writes can take a few seconds, and
    VideoDiscoveryFunction's own Timeout is 280s (see template.yaml's
    comment on the real per-minute YouTube rate-limit incident that
    number is sized for) -- far past what's reasonable to block
    AdminApiFunction's own request/response cycle on. Same soft-fail
    convention as queue_rescrape: returns {"queued": False, "reason"} --
    not an exception -- when VIDEO_DISCOVERY_FUNCTION_NAME isn't
    configured on this deployment, so a caller can build a graceful
    response instead of a 500.

    Raises LookupError if product_id itself doesn't exist -- same
    "caller error, not an expected outcome" distinction queue_rescrape
    draws for its own not-found case."""
    with conn.cursor() as cur:
        cur.execute("select id from products where id = %s", (product_id,))
        if cur.fetchone() is None:
            raise LookupError(f"No product with id {product_id}")

    function_name = os.environ.get("VIDEO_DISCOVERY_FUNCTION_NAME")
    if not function_name:
        return {"queued": False, "reason": "VIDEO_DISCOVERY_FUNCTION_NAME is not configured on this deployment"}

    import boto3

    lambda_client = boto3.client("lambda")
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps({"product_ids": [product_id]}),
    )
    return {"queued": True, "product_id": product_id}


def queue_video_stats_refresh(limit: int = None) -> dict:
    """On-demand "re-pull view/like/comment counts" trigger (POST
    /admin/refresh-video-stats) -- Al: "for the videos can we get pull
    down more data points from the videos, date it was added current view
    counts and any other data that make sense." View counts go stale
    immediately (unlike title/published_at, which are fixed facts once
    recorded), so this exists to let existing product_videos rows --
    not just newly-discovered ones -- get refreshed on demand.

    Same shape as queue_video_discovery immediately above: invokes
    VideoDiscoveryFunction directly (InvocationType='Event', async/fire-
    and-forget) rather than publishing to a queue, since there isn't one
    in front of that function, and the same soft-fail convention
    ({"queued": False, "reason": ...} instead of a 500) when
    VIDEO_DISCOVERY_FUNCTION_NAME isn't configured. Unlike queue_video_
    discovery, this is catalog-wide by design (see video_discovery.
    refresh_video_stats/select_video_ids_needing_stats_refresh for how it
    picks which rows) rather than scoped to one product_id, so it takes
    no conn and does no existence check -- there's no single row whose
    absence would make this a 404 the way a bad product_id would.
    limit=None lets VideoDiscoveryFunction fall back to its own
    DEFAULT_REFRESH_STATS_LIMIT rather than this layer needing to know
    that number too."""
    function_name = os.environ.get("VIDEO_DISCOVERY_FUNCTION_NAME")
    if not function_name:
        return {"queued": False, "reason": "VIDEO_DISCOVERY_FUNCTION_NAME is not configured on this deployment"}

    import boto3

    payload = {"refresh_stats": True}
    if limit is not None:
        payload["limit"] = limit

    lambda_client = boto3.client("lambda")
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload),
    )
    return {"queued": True, "limit": limit}


def set_product_published(conn, product_id: str, published: bool) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "update products set published = %s, updated_at = now() where id = %s returning id",
            (published, product_id),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product with id {product_id}")
    conn.commit()
    return {"product_id": product_id, "published": published}


# ---------------------------------------------------------------------
# Product image curation (migration 010) -- looking ahead to an eventual
# customer-facing site, Al: "once we actually have a customer facing site
# we will want to order the images, set a thumbnail image and control
# visibility." Nothing about this touches the scraper side (display_order/
# is_thumbnail/is_visible are purely admin-curated -- upsert_product's
# on-conflict path for product_images only ever touches image_type, same
# "raw scraped data vs. admin-curated data" split as coverstock_name vs.
# coverstocks.name).
# ---------------------------------------------------------------------

def update_product_image(conn, product_id: str, image_id: str, is_visible: bool = None,
                          is_thumbnail: bool = None) -> dict:
    """Partial update -- only the fields actually passed (not None) get
    written, same convention as this file's other set_*/update_* helpers.
    Both fields are independent toggles a caller can set in the same call
    or separately.

    is_thumbnail=True is handled as an atomic "make this the one
    thumbnail for this product" operation, not a bare column write:
    migration 010's partial unique index (`... where is_thumbnail`)
    enforces at most one true row per product_id, so setting a second row
    true without first clearing the old one would violate that constraint
    -- this unsets every other image on the same product_id inside the
    same transaction before setting the requested row, so the two
    UPDATEs commit together or not at all rather than racing each other
    across two separate admin_api calls. is_thumbnail=False is a plain
    single-row write (unsetting the current thumbnail, leaving the
    product with none, is allowed -- a caller can immediately set a
    different row true in a follow-up call).

    Raises LookupError if image_id doesn't exist or doesn't belong to
    product_id -- scoping the WHERE clause to both (not just image_id)
    means a caller can never accidentally mutate a different product's
    image by passing a mismatched pair."""
    with conn.cursor() as cur:
        if is_thumbnail is True:
            cur.execute(
                "update product_images set is_thumbnail = false where product_id = %s and id <> %s",
                (product_id, image_id),
            )

        set_clauses = []
        params = []
        if is_visible is not None:
            set_clauses.append("is_visible = %s")
            params.append(is_visible)
        if is_thumbnail is not None:
            set_clauses.append("is_thumbnail = %s")
            params.append(is_thumbnail)

        if not set_clauses:
            # Nothing to change -- still confirm the row exists/belongs to
            # this product, same not-found behavior as a real update would
            # give, rather than silently succeeding on a no-op.
            cur.execute(
                "select id from product_images where id = %s and product_id = %s",
                (image_id, product_id),
            )
            if cur.fetchone() is None:
                raise LookupError(f"No image {image_id} on product {product_id}")
            conn.commit()
            return {"image_id": image_id, "product_id": product_id}

        params += [image_id, product_id]
        cur.execute(
            f"update product_images set {', '.join(set_clauses)} where id = %s and product_id = %s returning id",
            params,
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No image {image_id} on product {product_id}")

    conn.commit()
    result = {"image_id": image_id, "product_id": product_id}
    if is_visible is not None:
        result["is_visible"] = is_visible
    if is_thumbnail is not None:
        result["is_thumbnail"] = is_thumbnail
    return result


# --------------------------------------------------------------------
# estimate_oil_motion / _reference_sku -- duplicated from public_api/
# service.py rather than shared (same "each Lambda is its own
# independent deployment package" reasoning as every other duplicated
# helper in this project -- see e.g. product_scraper.publish_messages'
# docstring). MUST stay in sync with public_api's copy: a visitor's
# plotter page and an admin's backfill run should never disagree about
# what a given core/coverstock combination estimates to. See public_api/
# service.py's module-level comment above its own estimate_oil_motion
# for the full reasoning behind every constant below, INCLUDING the
# 2026-08-14 refit against 40 real chart-matched products (Al: "i feel
# like it is way off for most balls") -- that comment has the full
# before/after accuracy numbers and per-constant reasoning; this copy
# only carries the resulting values.
# --------------------------------------------------------------------

OIL_BASE_BY_MATERIAL = {
    "polyester_plastic": 2,
    "urethane": 6,
    "reactive_resin": 10,
}
OIL_ADJUST_BY_TYPE = {
    "pearl": -3,
    "hybrid": 0,
    "solid": 0,
}
OIL_PARTICLE_BONUS = 2

MOTION_BASE_BY_CORE_TYPE = {
    "symmetric": 4,
    "asymmetric": 8,
}
MOTION_BASE_UNKNOWN_CORE = 6
MOTION_DIFF_MIDPOINT = 0.02
MOTION_DIFF_SCALE = 0.045
MOTION_DIFF_WEIGHT = 8
MOTION_ADJUST_BY_COVERSTOCK_TYPE = {
    "pearl": 2,
    "solid": 1,
    "hybrid": 0,
}

OIL_MIN, OIL_MAX = 1, 16
MOTION_MIN, MOTION_MAX = 1, 18


def _clamp_oil_motion(value: float, low: int, high: int) -> int:
    return max(low, min(high, round(value)))


def estimate_oil_motion(core_type: str = None, coverstock_type: str = None,
                         coverstock_material: str = None, has_particle: bool = False,
                         differential: float = None) -> dict:
    """Pure function, identical logic to public_api.service.estimate_oil_
    motion -- see that module for the full reasoning. Duplicated (not
    imported) since admin_api and public_api are separate Lambda
    packages."""
    oil = OIL_BASE_BY_MATERIAL.get(coverstock_material, (OIL_MIN + OIL_MAX) / 2)
    oil += OIL_ADJUST_BY_TYPE.get(coverstock_type, 0)
    if has_particle:
        oil += OIL_PARTICLE_BONUS
    oil = _clamp_oil_motion(oil, OIL_MIN, OIL_MAX)

    motion = MOTION_BASE_BY_CORE_TYPE.get(core_type, MOTION_BASE_UNKNOWN_CORE)
    if differential is not None:
        motion += ((float(differential) - MOTION_DIFF_MIDPOINT) / MOTION_DIFF_SCALE) * MOTION_DIFF_WEIGHT
    motion += MOTION_ADJUST_BY_COVERSTOCK_TYPE.get(coverstock_type, 0)
    motion = _clamp_oil_motion(motion, MOTION_MIN, MOTION_MAX)

    return {"oil": oil, "motion": motion}


def _reference_sku(skus: list):
    """Same 15lb-preferred convention as public_api._reference_sku -- see
    that function's docstring. skus is a list of dicts with at least
    weight_lbs/differential, pre-filtered to non-null differential."""
    if not skus:
        return None
    for sku in skus:
        if sku["weight_lbs"] == 15:
            return sku
    return min(skus, key=lambda s: abs(s["weight_lbs"] - 15))


def set_plotter_position(conn, product_id: str, oil_rating: int, motion_rating: int,
                          source: str = "manual") -> dict:
    """Writes products.oil_rating/motion_rating/oil_motion_source
    (migrations 011/012). source defaults to 'manual' -- this endpoint's
    main real-world caller is an admin correcting an estimate to
    something more accurate (Al's own ask: "adjusted in the admin api to
    a value that is more accurate if necessary"), so that's the sensible
    default rather than requiring every manual PATCH call to also pass
    source explicitly. scripts/backfill_plotter_chart_positions.py is the
    one caller that passes source='chart' explicitly.

    Both oil_rating/motion_rating are required together (not
    independently-optional like update_product_image's fields) -- a
    plotter position is meaningless with only one axis set. Range
    validation (1-16 / 1-18) happens at the database level via migration
    011's own CHECK constraints; a caller passing an out-of-range value
    gets a real psycopg2 error rather than this function silently
    clamping or guessing what was meant.

    Raises LookupError if product_id doesn't exist, same not-found
    convention as every other single-row setter in this module."""
    with conn.cursor() as cur:
        cur.execute(
            "update products set oil_rating = %s, motion_rating = %s, oil_motion_source = %s "
            "where id = %s returning id",
            (oil_rating, motion_rating, source, product_id),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product {product_id}")
    conn.commit()
    return {"product_id": product_id, "oil_rating": oil_rating, "motion_rating": motion_rating, "oil_motion_source": source}


def backfill_estimated_plotter_positions(conn) -> dict:
    """One-time (but idempotent, safe to re-run) catalog-wide backfill for
    every product with no plotter position at all yet -- Al's direct ask,
    after finding out the estimate was being recomputed live on every
    plotter API call: "i would prefer for it to just back fill the values
    once in the DB and then estimate on scrape if not set". This function
    is the "once" half; each scraper's upsert_product now has its own
    matching hook that covers "on scrape if not set" for anything scraped
    from here on. This function exists for whatever predates that hook --
    every product already in the catalog the moment this migration/
    deploy lands.

    Never touches a product that already has ANY plotter position (chart
    match, an earlier estimate, or a manual correction) -- 'where
    oil_rating is null' on the write is the same not-clobber guard as
    every scraper's own hook and as backfill_last_video_discovery_at
    above. Scoped to every product regardless of published/status -- a
    plotter position is scrape-derived metadata, not a publish-gated
    feature, so a product still under review gets a real position ready
    for whenever it's published, same reasoning the scrapers' hook uses.

    Two passes (read missing + their SKUs, then write), same shape as
    backfill_last_video_discovery_at above and for the same reason: stays
    correct even if a real scrape lands an estimate for one of these
    products between this function's SELECT and its UPDATE -- that
    product's oil_rating is no longer null by the time the UPDATE's WHERE
    clause runs, so it's naturally skipped instead of overwritten."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select p.id, c.core_type, p.coverstock_type, p.coverstock_material, p.has_particle
            from products p
            left join cores c on c.id = p.core_id
            where p.oil_rating is null
            """
        )
        columns = [desc[0] for desc in cur.description]
        missing = [dict(zip(columns, row)) for row in cur.fetchall()]

        product_ids = [p["id"] for p in missing]
        skus_by_product = {}
        if product_ids:
            cur.execute(
                "select product_id, weight_lbs, differential from product_skus "
                "where product_id = any(%s::uuid[]) and differential is not null",
                (product_ids,),
            )
            sku_columns = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                sku = dict(zip(sku_columns, row))
                skus_by_product.setdefault(sku["product_id"], []).append(sku)

    updated = 0
    with conn.cursor() as cur:
        for p in missing:
            ref_sku = _reference_sku(skus_by_product.get(p["id"], []))
            estimate = estimate_oil_motion(
                core_type=p["core_type"], coverstock_type=p["coverstock_type"],
                coverstock_material=p["coverstock_material"], has_particle=p["has_particle"],
                differential=ref_sku["differential"] if ref_sku else None,
            )
            cur.execute(
                "update products set oil_rating = %s, motion_rating = %s, oil_motion_source = 'estimated' "
                "where id = %s and oil_rating is null returning id",
                (estimate["oil"], estimate["motion"], p["id"]),
            )
            if cur.fetchone() is not None:
                updated += 1
    conn.commit()
    return {"products_missing_position": len(missing), "products_updated": updated}


def reestimate_plotter_positions(conn) -> dict:
    """Real ask, Al: "i feel like it is way off for most balls" -- backed
    up by DEPLOY_RUNBOOK.md 6m's spot-check against the 32 chart-matched
    products, which found estimate_oil_motion's original constants only
    landed 2/32 exact oil matches (mean error 3.3/16). Once those
    constants were refit against that real data (see this file's own
    estimate_oil_motion header for the refit), backfill_estimated_
    plotter_positions above is the wrong tool to apply the fix catalog-
    wide: it only ever fills a NULL position once and never revisits a
    row, so every product estimated under the OLD, badly-miscalibrated
    formula would keep that wrong value forever even after the formula
    itself was fixed.

    This is the one-time "go re-run the new formula over everything the
    old one already got wrong" pass -- run it once, right after this fix
    deploys. Not needed again after that: every NEWLY estimated product
    from here on already uses the refit constants (same estimate_oil_
    motion function, no separate code path for old vs. new), so there's
    nothing left to reconcile going forward.

    Scoped strictly to oil_motion_source = 'estimated' -- never touches
    'chart' (Brunswick's own authoritative published data, migration 011)
    or 'manual' (an admin's own correction, more trustworthy than any
    formula by definition). The UPDATE re-checks oil_motion_source =
    'estimated' at write time, not just at the initial SELECT, so a
    product that got manually corrected or matched onto a chart position
    in between is safely skipped instead of clobbered -- same two-pass,
    recheck-on-write shape as backfill_estimated_plotter_positions
    above, and oil_motion_source itself is left as 'estimated' (still an
    estimate, just a better one now)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select p.id, c.core_type, p.coverstock_type, p.coverstock_material, p.has_particle
            from products p
            left join cores c on c.id = p.core_id
            where p.oil_motion_source = 'estimated'
            """
        )
        columns = [desc[0] for desc in cur.description]
        estimated = [dict(zip(columns, row)) for row in cur.fetchall()]

        product_ids = [p["id"] for p in estimated]
        skus_by_product = {}
        if product_ids:
            cur.execute(
                "select product_id, weight_lbs, differential from product_skus "
                "where product_id = any(%s::uuid[]) and differential is not null",
                (product_ids,),
            )
            sku_columns = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                sku = dict(zip(sku_columns, row))
                skus_by_product.setdefault(sku["product_id"], []).append(sku)

    updated = 0
    with conn.cursor() as cur:
        for p in estimated:
            ref_sku = _reference_sku(skus_by_product.get(p["id"], []))
            estimate = estimate_oil_motion(
                core_type=p["core_type"], coverstock_type=p["coverstock_type"],
                coverstock_material=p["coverstock_material"], has_particle=p["has_particle"],
                differential=ref_sku["differential"] if ref_sku else None,
            )
            cur.execute(
                "update products set oil_rating = %s, motion_rating = %s "
                "where id = %s and oil_motion_source = 'estimated' returning id",
                (estimate["oil"], estimate["motion"], p["id"]),
            )
            if cur.fetchone() is not None:
                updated += 1
    conn.commit()
    return {"products_estimated": len(estimated), "products_updated": updated}


def reorder_product_images(conn, product_id: str, image_ids: list) -> dict:
    """Rewrites display_order to match the position of each id in
    image_ids (0-based) -- the admin-site "move up/move down" controls
    reorder the array client-side and resubmit the whole list rather than
    sending incremental swaps, which sidesteps any question of what two
    concurrent partial-swap calls should do to each other.

    Scoped to product_id the same way update_product_image is: an id in
    image_ids that doesn't actually belong to product_id is silently
    ignored (not applied, not an error) -- this only ever touches rows
    that are both in the list AND belong to this product, so a stray/
    mistyped id from a stale client-side list can't repoint another
    product's image ordering. Any of this product's own images NOT
    present in image_ids keep their existing display_order untouched --
    callers are expected to pass the full current list (that's what the
    admin-site UI always does, since it starts from the just-loaded
    image set), but a partial list is handled gracefully rather than
    raising, since a caller reordering a product with images added by a
    concurrent rescrape mid-edit shouldn't discover that as an error."""
    with conn.cursor() as cur:
        for position, image_id in enumerate(image_ids):
            cur.execute(
                "update product_images set display_order = %s where id = %s and product_id = %s",
                (position, image_id, product_id),
            )
    conn.commit()
    return {"product_id": product_id, "image_ids": image_ids}


# ---------------------------------------------------------------------
# Video candidates (YouTube content enrichment). Same approve/reject shape
# as review_queue above, but a dedicated table -- see
# db/migrations/004_product_videos.sql's comment for why product_videos
# isn't just reusing review_queue's field_name/current_value/proposed_value
# shape. Approving a candidate here has a side effect the review_queue
# approve doesn't: it publishes an SQS message so video_summarizer picks up
# the transcript+Bedrock-summary work, rather than applying a value
# directly.
# ---------------------------------------------------------------------

def list_video_candidates(conn, status: str = "pending", product_id: str = None, limit: int = 50, offset: int = 0) -> list:
    """Real bug found via a live full-catalog run of
    scripts/auto_approve_video_candidates.py: a single video_discovery
    invocation inserts many product_videos rows in quick succession, often
    with identical or near-identical `created_at` timestamps -- ordering
    by (match_confidence, created_at) alone has no way to break those
    ties deterministically, so OFFSET/LIMIT pagination across multiple
    calls (this function is paginated by both that script and
    home_transcript_fetcher.py) could return the same row on two different
    pages (observed live: one candidate got approved, then failed with a
    real 422 the second time it showed up) and, by the same instability,
    could just as easily have skipped a different row entirely without any
    visible error. `pv.id` is added as a final, always-unique tiebreaker so
    the ordering -- and therefore the pagination -- is fully deterministic.

    status=None omits the status filter entirely -- added for the product
    detail view's "Videos" section (see admin-site's loadProductDetailInto),
    which deliberately wants to show a product's candidates across every
    status (pending/approved/rejected), not just one. Real motivating case,
    Al: "the combat solid has a bunch of videos approved for it that are
    for the original combat and combat hybrid but the new videos for it
    are not there" -- exactly the known false-positive shape
    reassign_video_candidate's own docstring already documents (score_match
    matches on ANY ONE product-name token, so "Combat"/"Combat Hybrid"
    review videos can score 'high' for the "Combat Solid" product too).
    Seeing approved/pending/rejected together, scoped to one product, is
    what actually lets an admin spot and fix that kind of mismatch -- the
    existing Video Candidates tab only ever shows one status at a time and
    isn't scoped to a product by default, so a bad reassignment like this
    could sit unnoticed indefinitely."""
    query = """
        select pv.id, pv.product_id, p.name as product_name, b.name as brand_name,
               pv.youtube_video_id, pv.title, pv.channel_title, pv.published_at,
               pv.thumbnail_url, pv.match_query, pv.match_confidence,
               pv.transcript_note, pv.status, pv.source,
               pv.created_at, pv.resolved_at, pv.resolved_by,
               pv.view_count, pv.like_count, pv.comment_count,
               pv.duration_seconds, pv.stats_fetched_at,
               (pv.summary is not null) as has_summary
        from product_videos pv
        join products p on p.id = pv.product_id
        join brands b on b.id = p.brand_id
    """
    params = []
    conditions = []
    if status is not None:
        conditions.append("pv.status = %s")
        params.append(status)
    if product_id:
        conditions.append("pv.product_id = %s")
        params.append(product_id)
    if conditions:
        query += " where " + " and ".join(conditions)
    query += " order by pv.match_confidence asc, pv.created_at asc, pv.id asc limit %s offset %s"
    params += [limit, offset]

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_video_candidate(conn, video_id: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            select pv.*, p.name as product_name, b.name as brand_name
            from product_videos pv
            join products p on p.id = pv.product_id
            join brands b on b.id = p.brand_id
            where pv.id = %s
            """,
            (video_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cur.description]
        return dict(zip(columns, row))


def get_pending_video_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("select count(*) from product_videos where status = 'pending'")
        return cur.fetchone()[0]


def approve_video_candidate(conn, video_id: str, resolved_by: str) -> dict:
    """Marks the candidate approved. Deliberately does NOT publish to
    VIDEO_SUMMARIZE_QUEUE_URL / video_transcript_fetcher anymore -- that
    was this function's original behavior, but it created a real race once
    the home browser fetcher (scripts/home_transcript_fetcher_browser.py)
    became the confirmed-working transcript path: video_transcript_fetcher
    is a plain-HTTP fetch and, per its own module docstring, is confirmed
    blocked by YouTube's PoToken/BotGuard requirement regardless of network
    path (VPC or not). It never raises on that -- get_transcript treats a
    blocked fetch as an expected outcome and always forwards a result with
    a transcript_note (e.g. "captions_listed_but_transcript_fetch_returned_
    empty") to video_summarizer, which writes that note onto the row.
    needs_transcript() (home_transcript_fetcher.py) only picks up rows
    where transcript_note IS NULL -- so if this function still auto-queued
    to that broken Lambda on every approval, every candidate would get a
    failure note written moments after approval, and the actually-working
    browser fetcher would skip it forever, having no way to tell "genuinely
    checked and no captions" apart from "never got a real attempt". Leaving
    transcript_note untouched at approval time is what lets the home
    browser cron (the confirmed-working path, see DEPLOY_RUNBOOK.md 6k) be
    the one and only thing that sets it. VideoTranscriptFetcherFunction and
    VideoSummarizeQueue are left deployed (nothing publishes to the queue
    now, so the function simply never fires) rather than torn out here --
    removing dead infra is a separate, lower-stakes cleanup, not bundled
    into this fix.

    Also selects youtube_video_id here (unused for publishing now, but kept
    since callers/tests may still want it, and it costs nothing extra in
    the same query)."""
    with conn.cursor() as cur:
        cur.execute("select status, youtube_video_id from product_videos where id = %s", (video_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_videos row with id {video_id}")
        if row[0] != "pending":
            raise ValueError(f"product_videos row {video_id} is already {row[0]}, not pending")

        cur.execute(
            "update product_videos set status = 'approved', resolved_at = now(), resolved_by = %s where id = %s",
            (resolved_by, video_id),
        )
    conn.commit()

    return {"video_id": video_id, "status": "approved"}


def reject_video_candidate(conn, video_id: str, resolved_by: str, reason: str = None) -> dict:
    with conn.cursor() as cur:
        cur.execute("select status from product_videos where id = %s", (video_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_videos row with id {video_id}")
        if row[0] != "pending":
            raise ValueError(f"product_videos row {video_id} is already {row[0]}, not pending")

        cur.execute(
            "update product_videos set status = 'rejected', resolved_at = now(), resolved_by = %s where id = %s",
            (resolved_by, video_id),
        )
    conn.commit()
    return {"video_id": video_id, "status": "rejected"}


def restore_video_candidate(conn, video_id: str) -> dict:
    """Undoes a mistaken approve/reject. Al: "it appears if i accidentally
    reject a video i can not undo that action" -- correct, and deliberately
    so up to this point: approve_video_candidate/reject_video_candidate both
    only allow a one-way pending -> approved / pending -> rejected
    transition (see their own guards), specifically so a bulk action or a
    stale UI double-click couldn't silently re-apply a decision. That same
    guard just never had a way back out. This is that way back: moves an
    already-resolved row (status IN ('approved', 'rejected')) back to
    'pending' and clears resolved_at/resolved_by, i.e. restores it to
    exactly the state a freshly-discovered candidate is in, so it shows
    back up in the normal pending approve/reject workflow for another look.

    No resolved_by parameter, unlike approve/reject/reassign -- there's
    nothing being resolved here (quite the opposite), so there's no
    decision to attribute; the row goes back to having no resolved_by at
    all, same as one that was never touched. Same reasoning
    delete_video_candidate uses for not taking one either.

    Restoring an already-pending row is a hard error, not a silent no-op --
    that'd usually mean the caller's UI state is stale (e.g. two admins
    both had the same row open), which is worth surfacing rather than
    papering over.

    Note for Shorts-filtered rows specifically (see video_discovery.
    apply_video_stats' force-reject and MIN_VIDEO_DURATION_SECONDS): a
    restored row that's still genuinely a Short will simply get
    auto-rejected again on its next scheduled stats refresh -- that logic
    re-checks duration on every refresh regardless of current status, so
    there's no special-casing needed here to keep it from silently
    resurfacing as 'pending' forever."""
    with conn.cursor() as cur:
        cur.execute("select status from product_videos where id = %s", (video_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_videos row with id {video_id}")
        if row[0] not in ("approved", "rejected"):
            raise ValueError(f"product_videos row {video_id} is {row[0]}, not approved or rejected -- nothing to restore")

        cur.execute(
            "update product_videos set status = 'pending', resolved_at = null, resolved_by = null where id = %s",
            (video_id,),
        )
    conn.commit()
    return {"video_id": video_id, "status": "pending"}


def reassign_video_candidate(conn, video_id: str, new_product_id: str, resolved_by: str = None) -> dict:
    """Moves a video candidate to a different product. Built for a real,
    known failure mode of video_discovery's score_match heuristic (see its
    module docstring): 'high' confidence only requires the brand name plus
    ANY ONE significant product-name token in the title, so e.g. "Storm
    Absolute Power Review" scores 'high' for the "Storm Absolute" product
    too, not just "Storm Absolute Power" -- a real, accepted tradeoff of
    auto-approving 'high' matches in bulk (see
    scripts/auto_approve_video_candidates.py's docstring), not something
    this function tries to prevent. This is the correction tool for when
    it happens: works regardless of status (pending/approved/rejected).

    Real report from Al, second-order bug found while cleaning up Combat
    Solid: reassigning used to just UPDATE the row's product_id in place.
    That frees up the origin product's (product_id, youtube_video_id)
    uniqueness slot, and insert_candidates' ON CONFLICT DO NOTHING (see
    video_discovery/app.py) only suppresses re-insertion when a row still
    occupies that slot -- so the exact same false-positive video came right
    back on the very next rescan of the origin product. Deleting instead of
    reassigning has the identical problem, for the identical reason (see
    delete_video_candidate's docstring above this one).

    Fix: reassigning now ALWAYS leaves a rejected tombstone behind at the
    origin (product_id, youtube_video_id) slot -- same status='rejected'
    update reject_video_candidate does, just applied directly here since
    reject_video_candidate itself only allows pending -> rejected and this
    must work from any starting status. That tombstone is what permanently
    blocks video_discovery from reinserting this video under the wrong
    product again. The actual content moves to the target product as
    either:
      - a brand-new product_videos row, carrying over title/channel/
        transcript/summary/status, if the target has no row for this
        youtube_video_id yet; or
      - a merge into the target's EXISTING row, if one already exists
        there (a real, legitimate case -- the target's own video_discovery
        run may have independently found the same video). No IntegrityError
        avoidance trick here: this used to require the admin to manually
        delete one of the two duplicates before retrying, which is exactly
        what caused the resurfacing bug in the first place, since deleting
        the origin row removed its blocking tombstone. Merging instead of
        erroring removes that whole manual step. The merge only backfills
        the target row's transcript/transcript_note/summary where they're
        currently null (so review work already done under the wrong
        product isn't lost) and never touches the target's own status --
        an admin who already reviewed the target's copy shouldn't have that
        judgment silently overwritten by a merge.

    resolved_by is optional and, if given, is stamped on the origin's
    tombstone (same field approve/reject use) purely for audit -- "who
    reassigned this away from here." It intentionally does NOT get stamped
    onto the target row when a fresh copy is inserted; that copy keeps
    whatever status (and therefore whatever resolved_by) it already had."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select product_id, youtube_video_id, title, channel_title, published_at,
                   thumbnail_url, match_query, match_confidence, transcript,
                   transcript_note, summary, status, source
            from product_videos where id = %s
            """,
            (video_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_videos row with id {video_id}")
        (origin_product_id, youtube_video_id, title, channel_title, published_at,
         thumbnail_url, match_query, match_confidence, transcript,
         transcript_note, summary, status, source) = row

        if origin_product_id == new_product_id:
            raise ValueError(f"product_videos row {video_id} is already assigned to product {new_product_id}")

        cur.execute("select id from products where id = %s", (new_product_id,))
        if cur.fetchone() is None:
            raise LookupError(f"No products row with id {new_product_id}")

        cur.execute(
            "select id, transcript, summary, status from product_videos where product_id = %s and youtube_video_id = %s",
            (new_product_id, youtube_video_id),
        )
        conflict = cur.fetchone()

        if conflict is None:
            cur.execute(
                """
                insert into product_videos
                    (product_id, youtube_video_id, title, channel_title, published_at,
                     thumbnail_url, match_query, match_confidence, transcript,
                     transcript_note, summary, status, source)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (new_product_id, youtube_video_id, title, channel_title, published_at,
                 thumbnail_url, match_query, match_confidence, transcript,
                 transcript_note, summary, status, source),
            )
            target_video_id = cur.fetchone()[0]
            merged_with_existing = False
        else:
            conflict_id, conflict_transcript, conflict_summary, _conflict_status = conflict
            backfill_columns = []
            backfill_values = []
            if conflict_transcript is None and transcript is not None:
                backfill_columns.append("transcript")
                backfill_values.append(transcript)
            if conflict_summary is None and summary is not None:
                backfill_columns.append("summary")
                backfill_values.append(summary)
                backfill_columns.append("transcript_note")
                backfill_values.append(transcript_note)
            if backfill_columns:
                set_clause = ", ".join(f"{col} = %s" for col in backfill_columns)
                cur.execute(
                    f"update product_videos set {set_clause} where id = %s returning id",
                    (*backfill_values, conflict_id),
                )
            target_video_id = conflict_id
            merged_with_existing = True

        # Tombstone the origin -- see this function's docstring. Identical
        # SQL text to reject_video_candidate's own update, applied directly
        # here (not via reject_video_candidate) because that function only
        # permits pending -> rejected and this must work from any status.
        cur.execute(
            "update product_videos set status = 'rejected', resolved_at = now(), resolved_by = %s where id = %s",
            (resolved_by, video_id),
        )
    conn.commit()
    return {
        "video_id": target_video_id,
        "product_id": new_product_id,
        "origin_video_id": video_id,
        "merged_with_existing": merged_with_existing,
    }


def delete_video_candidate(conn, video_id: str) -> dict:
    """Hard delete -- distinct from reject_video_candidate, which only
    marks status='rejected' and keeps the row for audit.

    CAUTION, and the reason reassign_video_candidate no longer uses this as
    its conflict-cleanup step: deleting a product_videos row frees up that
    row's (product_id, youtube_video_id) uniqueness slot, and
    insert_candidates' ON CONFLICT DO NOTHING (video_discovery/app.py) only
    suppresses re-insertion while a row still occupies that slot. Delete a
    row to get rid of a wrong video, and the very next rescan of that
    product can bring the exact same video right back as a fresh 'pending'
    candidate -- a real, reported bug (see reassign_video_candidate's
    docstring for the full story). Prefer reject_video_candidate (or
    reassign_video_candidate, which now tombstones the origin
    automatically) for "this video doesn't belong here" -- both leave a
    row behind that blocks reinsertion. Reserve this function for true
    duplicate cleanup: two DIFFERENT products can legitimately each hold
    their own row for the same YouTube video (one review can genuinely
    cover two products), and reassign_video_candidate's merge path can
    still leave a stale extra copy in rare hand-edited cases -- deleting
    the redundant copy there is safe, since the video's real slot (on
    whichever product actually keeps it) is still occupied by the
    surviving row."""
    with conn.cursor() as cur:
        cur.execute("select id from product_videos where id = %s", (video_id,))
        if cur.fetchone() is None:
            raise LookupError(f"No product_videos row with id {video_id}")
        cur.execute("delete from product_videos where id = %s", (video_id,))
    conn.commit()
    return {"video_id": video_id, "deleted": True}


# _publish_video_summarize_message (published to VIDEO_SUMMARIZE_QUEUE_URL
# for video_transcript_fetcher) used to live here and was called from
# approve_video_candidate -- removed rather than left as dead code once
# approve_video_candidate stopped calling it (see that function's
# docstring for why). VIDEO_SUMMARIZE_QUEUE_URL/VideoSummarizeQueue are
# still wired in template.yaml; nothing publishes to that queue anymore,
# so VideoTranscriptFetcherFunction simply never fires. Tearing that infra
# out is a separate cleanup, not bundled into this fix.


def submit_video_transcript(conn, video_id: str, transcript: str, transcript_note: str = None) -> dict:
    """Publishes an externally-fetched transcript straight to
    VideoTranscriptResultQueue -- the same queue video_transcript_fetcher
    publishes to -- so video_summarizer picks it up and does the DB write +
    Bedrock call exactly like it would for a Lambda-fetched transcript, no
    special-casing downstream. This is the real reason this endpoint exists:
    live testing this session found YouTube's caption-fetch behavior
    identical (blocked) from both a VPC-attached and a non-VPC Lambda, but
    working from a residential connection -- see
    src/video_transcript_fetcher/app.py's module docstring for the full
    evidence trail, and scripts/home_transcript_fetcher.py for the
    residential-side counterpart that calls this endpoint, meant to run on
    the user's own hardware at home rather than in AWS.

    Only requires the row exist and already be 'approved' -- the same gate
    video_summarizer's own _process_one applies -- so this can't be used to
    inject a transcript onto a row that's still pending review or was
    rejected. Deliberately does NOT soft-fail like
    _publish_video_summarize_message does when its queue isn't configured
    (that function has a DB write to fall back on; this one's entire job
    IS the publish, there's nothing else to persist) -- a missing
    TRANSCRIPT_RESULT_QUEUE_URL is a real deployment misconfiguration, so
    it's left to raise (KeyError, surfaced as a 500) rather than silently
    discarding the caller's transcript."""
    with conn.cursor() as cur:
        cur.execute("select status from product_videos where id = %s", (video_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_videos row with id {video_id}")
        if row[0] != "approved":
            raise ValueError(
                f"product_videos row {video_id} is {row[0]}, not approved -- can't submit a transcript for it"
            )

    _publish_transcript_result_message(video_id, transcript, transcript_note)
    return {"video_id": video_id, "queued_for_summary": True}


def _publish_transcript_result_message(product_video_id: str, transcript: str, transcript_note: str) -> None:
    queue_url = os.environ["TRANSCRIPT_RESULT_QUEUE_URL"]

    import boto3

    sqs = boto3.client("sqs")
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({
            "product_video_id": product_video_id,
            "transcript": transcript,
            "transcript_note": transcript_note,
        }),
    )


# ---------------------------------------------------------------------
# "Summary of summaries" on-demand refresh (POST /products/{id}/refresh-
# video-summary). video_summarizer.refresh_video_reviews_rollup already
# regenerates products.video_reviews_summary automatically every time a
# video gets a real summary written -- this is the on-demand counterpart,
# for the cases that trigger doesn't cover: backfilling a product whose
# videos were already summarized before this endpoint existed, or
# re-running it after a manual reassign/delete cleanup changed which
# videos count as "approved" for a product without anything re-
# summarizing.
#
# fetch_approved_video_summaries / build_rollup_prompt / generate_video_
# reviews_rollup / store_rollup below are deliberate duplicates of
# video_summarizer/app.py's functions of the same name, NOT imports --
# admin_api and video_summarizer are separate Lambda deployment packages
# (separate CodeUri, no shared module path between them), same "own the
# whole package" convention as scripts/home_transcript_fetcher.py
# duplicating video_transcript_fetcher/app.py's YouTube-fetching logic
# (see that script's module docstring for the fuller reasoning). Keep
# both copies in sync if the prompt or Bedrock wire format ever changes.
# ---------------------------------------------------------------------

DEFAULT_BEDROCK_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_ROLLUP_MAX_TOKENS = 350


def fetch_approved_video_summaries(conn, product_id: str) -> list:
    with conn.cursor() as cur:
        cur.execute(
            """
            select summary from product_videos
            where product_id = %s and status = 'approved' and summary is not null
            order by created_at asc, id asc
            """,
            (product_id,),
        )
        return [row[0] for row in cur.fetchall()]


def build_rollup_prompt(product_name: str, brand_name: str, summaries: list, description: str = None) -> str:
    """Kept in sync with video_summarizer/app.py's function of the same
    name -- see this file's module comment above fetch_approved_video_
    summaries for why these are deliberate duplicates, not a shared import.

    description (optional): the manufacturer's own marketing copy for this
    ball, scraped from its product page (products.description). Included
    as grounding context when present -- useful for getting technical
    details right (core/coverstock names, the lane conditions it's
    marketed for) -- but the prompt is explicit that the output must still
    reflect what reviewers actually said, not just restate marketing copy."""
    context_block = ""
    if description:
        context_block = (
            "\n\nFor context, here is the manufacturer's own description of "
            "this ball. Use it to get technical details right (core/"
            "coverstock names, the lane conditions it's marketed for), but "
            "the summary must still reflect what reviewers actually said, "
            "not just restate marketing copy:\n" + description
        )

    if len(summaries) == 1:
        return (
            f"The following is a summary of a single YouTube review video for the "
            f"{brand_name} {product_name} bowling ball. Rewrite it as a standalone "
            "2-4 sentence product description of what reviewers say about this ball "
            "-- remove any references to \"this video\" or \"the reviewer\", state it "
            "as plain fact about the ball's performance instead."
            f"{context_block}"
            f"\n\nReview summary:\n{summaries[0]}"
        )

    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(summaries, start=1))
    return (
        f"The following are {len(summaries)} independent review summaries for the "
        f"{brand_name} {product_name} bowling ball, each from a different YouTube "
        "review video. Synthesize them into a single 3-5 sentence overview of what "
        "reviewers generally say about this ball -- note common themes (hook shape, "
        "reaction on the lane, who it's recommended for) and call out any notable "
        "disagreements between reviewers rather than papering over them. Don't "
        "reference \"the videos\" or how many reviews there are; write it as a "
        "standalone product description."
        f"{context_block}"
        f"\n\nReview summaries:\n{numbered}"
    )


def generate_video_reviews_rollup(bedrock_client, model_id: str, product_name: str, brand_name: str,
                                   summaries: list, description: str = None,
                                   max_tokens: int = DEFAULT_ROLLUP_MAX_TOKENS) -> str:
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [
            {"role": "user", "content": build_rollup_prompt(product_name, brand_name, summaries, description)},
        ],
    })
    response = bedrock_client.invoke_model(modelId=model_id, contentType="application/json",
                                            accept="application/json", body=body)
    payload = json.loads(response["body"].read())
    return payload["content"][0]["text"].strip()


def store_rollup(conn, product_id: str, rollup_text: str, video_count: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update products
            set video_reviews_summary = %s,
                video_reviews_summary_video_count = %s,
                video_reviews_summary_updated_at = now()
            where id = %s
            """,
            (rollup_text, video_count, product_id),
        )
    conn.commit()


def _fetch_product_for_rollup(conn, product_id: str):
    with conn.cursor() as cur:
        cur.execute(
            "select p.id, p.name, b.name as brand_name, p.description from products p "
            "join brands b on b.id = p.brand_id where p.id = %s",
            (product_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"id": row[0], "name": row[1], "brand_name": row[2], "description": row[3]}


def refresh_video_reviews_rollup(conn, product_id: str) -> dict:
    """Builds its own Bedrock client and reads BEDROCK_MODEL_ID itself
    (same env var video_summarizer uses) rather than taking either as a
    parameter, so app.py's endpoint stays a thin routing call with nothing
    to construct -- consistent with every other service.py function here
    (e.g. _publish_transcript_result_message building its own boto3 SQS
    client). 'no approved+summarized videos yet' is a normal, expected
    outcome (rollup_regenerated: False), not an error -- same convention
    as video_summarizer's own version of this function."""
    product = _fetch_product_for_rollup(conn, product_id)
    if product is None:
        raise LookupError(f"No products row with id {product_id}")

    summaries = fetch_approved_video_summaries(conn, product_id)
    if not summaries:
        return {"product_id": product_id, "rollup_regenerated": False, "reason": "no_summaries"}

    import boto3

    bedrock_client = boto3.client("bedrock-runtime")
    model_id = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL_ID)
    rollup_text = generate_video_reviews_rollup(
        bedrock_client, model_id, product["name"], product["brand_name"], summaries,
        product["description"],
    )
    store_rollup(conn, product_id, rollup_text, len(summaries))
    return {"product_id": product_id, "rollup_regenerated": True, "video_count": len(summaries)}


# ---------------------------------------------------------------------
# One-off correction for migration 005 (last_video_discovery_at) not
# backfilling existing data -- see that migration's own comment and
# src/video_discovery/app.py's module docstring, ROTATION section.
# ---------------------------------------------------------------------

def backfill_last_video_discovery_at(conn) -> dict:
    """Real gap found in production: migration 005 added products.
    last_video_discovery_at with no backfill, so any product searched
    before that migration ran (under the old, buggy `updated_at desc`
    rotation -- see video_discovery's ROTATION incident writeup) has real
    product_videos rows but a NULL last_video_discovery_at. Since
    rotation now sorts NULLs first, those already-searched products kept
    jumping the queue ahead of products that had genuinely never been
    searched -- caught via a live count mismatch (231 with a NULL column
    vs. only 174 with zero product_videos rows at all, a ~57-product
    gap).

    Idempotent and safe to re-run: only ever sets a currently-NULL
    column, and only for a product that actually has product_videos
    history -- a genuinely never-searched product's column stays NULL
    exactly as intended, so it still sorts first under video_discovery's
    rotation ordering. Uses each product's EARLIEST product_videos.
    created_at (not the latest) -- the goal is "when did this product
    first get covered", matching what mark_product_searched would have
    recorded if it had existed at the time, not "when was it most
    recently touched".

    Two queries rather than one UPDATE ... FROM: keeps the read (which
    products have earliest search history) separate from the write (only
    touching rows still NULL), which stays correct even if a real
    video_discovery invocation searches one of these same products
    between this function's SELECT and its UPDATE -- that product's
    column is no longer NULL by the time the second query's WHERE clause
    runs, so it's naturally skipped instead of being overwritten with a
    stale backfilled value."""
    with conn.cursor() as cur:
        cur.execute(
            "select product_id, min(created_at) from product_videos group by product_id"
        )
        earliest_by_product = {row[0]: row[1] for row in cur.fetchall()}

    updated = 0
    with conn.cursor() as cur:
        for product_id, earliest in earliest_by_product.items():
            cur.execute(
                "update products set last_video_discovery_at = %s "
                "where id = %s and last_video_discovery_at is null "
                "returning id",
                (earliest, product_id),
            )
            if cur.fetchone() is not None:
                updated += 1
    conn.commit()
    return {"products_with_video_history": len(earliest_by_product), "products_updated": updated}


# ---------------------------------------------------------------------
# One-off correction for the MOTIV/netsuite status bug -- see
# src/netsuite_product_scraper/app.py's module docstring "REAL INCIDENT"
# section for the full root-cause writeup. Same shape as
# backfill_last_video_discovery_at above: a single bulk server-side
# correction, not a per-product loop, fixing rows that already went wrong
# before the actual code fix (netsuite_product_scraper's new
# get_status_for_url fallback) landed.
# ---------------------------------------------------------------------

def backfill_netsuite_status(conn) -> dict:
    """Corrects products.status for every netsuite-platform (MOTIV) row
    that was silently clobbered to 'current' by the queue_rescrape bug --
    see this module's own queue_rescrape and netsuite_product_scraper's
    module docstring for the full mechanism. discovered_urls.status_path
    is the ground truth (netsuite_url_discovery classifies it correctly at
    discovery time, confirmed live: 60 current/374 retired vs. products
    showing 202/202 'current'); this does one bulk UPDATE ... FROM,
    matched by url, rather than a per-product loop -- there's no per-row
    decision to make, every mismatch is corrected the same way.

    Scoped to source_platform = 'netsuite': this is a targeted fix for the
    one platform this bug actually hit (see netsuite_product_scraper's
    docstring for why NetSuite specifically was exposed -- no on-page
    status signal AND upsert_product's non-coalescing status overwrite),
    not a blanket "trust discovered_urls over products" rule applied
    catalog-wide.

    Idempotent and safe to re-run: the WHERE clause only matches rows that
    still disagree with discovered_urls, so a second run naturally
    corrects nothing further (products_corrected: 0) rather than
    re-touching already-fixed rows. Does NOT touch any product whose url
    has no matching discovered_urls row (e.g. a manually-inserted product)
    -- there's no ground truth to correct it against, so it's left alone
    rather than guessed at."""
    with conn.cursor() as cur:
        cur.execute(
            """
            update products p
            set status = du.status_path, updated_at = now()
            from discovered_urls du
            where du.url = p.url
              and p.source_platform = 'netsuite'
              and du.status_path is not null
              and du.status_path <> p.status
            returning p.id
            """
        )
        corrected_ids = [row[0] for row in cur.fetchall()]
    conn.commit()
    return {"products_corrected": len(corrected_ids)}


# ---------------------------------------------------------------------
# Price tracking (migration 014/015) -- Al: "id like to start a price
# tracker. this should be configurable to have site setup so that it
# will pull the current price from a number of sites on a frequency of
# likely daily? then store this in a way that would allow for charting
# that price over time in the admin ui and eventually the consumer UI."
#
# DESIGN CORRECTION, mid-build (see 014_price_tracking.sql's header
# comment for the full writeup): "site setup" means choosing which real
# retailers to track (bowling.com, bowlingball.com, bowlersmart.com,
# ...), with each product's URL on each site found AUTOMATICALLY by
# price_checker's discovery job (mirroring video_discovery's YouTube
# search), not typed in by an admin. And after weighing auto-track-
# immediately against a pending-review gate, Al settled on "the
# reccomended path is best": mirror product_videos' pending/approved/
# rejected review workflow exactly (see list_price_sources/
# approve_price_source/reject_price_source/restore_price_source below),
# including undo/restore, built in from the start here rather than added
# later the way restore_video_candidate was.
#
# This section is the admin-facing half: managing the price_sites
# registry (including each site's search config), reviewing/resolving
# discovery candidates, a manual-override path for when a search doesn't
# find a real match, reading history back out for charting, and
# triggering price_checker on demand -- price_checker itself (the actual
# search/fetch/parse/record logic) lives in its own Lambda, same split
# as VideoDiscoveryFunction/video_discovery vs. this file's
# queue_video_discovery/queue_video_stats_refresh above.
# ---------------------------------------------------------------------

def list_price_sites(conn) -> list:
    """Every configured retailer site, active or not -- the admin UI's
    Price Sites tab needs to show inactive ones too (so they can be
    re-activated), unlike most other list_* filters in this file that
    default to hiding inactive/rejected/retired rows.

    fetch_method/api_provider/base_url (016_price_tracking_bigcommerce.sql)
    let the admin UI show/edit an 'api' site's different config shape
    (no search_url_template/result_link_selector/default_css_selector --
    those are nullable now, see that migration -- but api_provider/
    base_url instead)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, name, search_url_template, result_link_selector,
                   default_css_selector, notes, is_active, created_at,
                   fetch_method, api_provider, base_url
            from price_sites
            order by name asc
            """
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0], "name": r[1], "search_url_template": r[2],
            "result_link_selector": r[3], "default_css_selector": r[4],
            "notes": r[5], "is_active": r[6], "created_at": r[7],
            "fetch_method": r[8], "api_provider": r[9], "base_url": r[10],
        }
        for r in rows
    ]


def create_price_site(conn, name: str, search_url_template: str = None, result_link_selector: str = None,
                       default_css_selector: str = None, notes: str = None,
                       fetch_method: str = "scrape", api_provider: str = None, base_url: str = None) -> dict:
    """Adding a new retailer site is just this -- one INSERT, no new
    Lambda/deploy. search_url_template + result_link_selector are the
    site-SEARCH config discovery uses to find candidate product URLs
    (see price_checker.search_site_for_product); default_css_selector is
    the price-page config checking uses once a candidate is approved.
    name is unique (migration 014's constraint) so a typo'd duplicate add
    surfaces as a clear IntegrityError rather than two confusingly-
    similar rows.

    fetch_method defaults to 'scrape' (016_price_tracking_bigcommerce.sql)
    -- every existing caller that doesn't know about the new column keeps
    creating a scrape site exactly as before. The three scrape-only
    fields are optional here (nullable in the DB now, but still REQUIRED
    for a 'scrape' row and api_provider REQUIRED for an 'api' row, per
    that migration's price_sites_fetch_method_fields_check) -- this
    function deliberately doesn't re-validate that combination itself,
    same "let the DB constraint be the source of truth for the field
    combination, surface as a clear IntegrityError" posture this project
    already takes elsewhere (e.g. price_sites.name's own unique
    constraint, right above)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into price_sites
                (name, search_url_template, result_link_selector, default_css_selector, notes,
                 fetch_method, api_provider, base_url)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (name, search_url_template, result_link_selector, default_css_selector, notes,
             fetch_method, api_provider, base_url),
        )
        site_id = cur.fetchone()[0]
    conn.commit()
    return {
        "id": site_id, "name": name, "search_url_template": search_url_template,
        "result_link_selector": result_link_selector, "default_css_selector": default_css_selector,
        "notes": notes, "fetch_method": fetch_method, "api_provider": api_provider, "base_url": base_url,
    }


def update_price_site(conn, site_id: str, name: str = None, search_url_template: str = None,
                       result_link_selector: str = None, default_css_selector: str = None,
                       notes: str = None, is_active: bool = None,
                       fetch_method: str = None, api_provider: str = None, base_url: str = None) -> dict:
    """Partial update, same not-None-means-set convention as
    update_product_image above. is_active=False is how a site gets
    retired without deleting it (and the product_price_sources/history
    rows that reference it) -- see delete_price_site for the actually-
    destructive option; it also stops the site from being searched on
    the next discovery pass (see price_checker.list_active_price_sites).

    fetch_method/api_provider/base_url (016_price_tracking_bigcommerce.sql)
    follow the same not-None-means-set convention as every other field
    here -- same DB-constraint-is-the-source-of-truth posture as
    create_price_site for validating the fetch_method/field combination,
    not re-checked in this layer."""
    with conn.cursor() as cur:
        set_clauses = []
        params = []
        if name is not None:
            set_clauses.append("name = %s")
            params.append(name)
        if search_url_template is not None:
            set_clauses.append("search_url_template = %s")
            params.append(search_url_template)
        if result_link_selector is not None:
            set_clauses.append("result_link_selector = %s")
            params.append(result_link_selector)
        if default_css_selector is not None:
            set_clauses.append("default_css_selector = %s")
            params.append(default_css_selector)
        if notes is not None:
            set_clauses.append("notes = %s")
            params.append(notes)
        if is_active is not None:
            set_clauses.append("is_active = %s")
            params.append(is_active)
        if fetch_method is not None:
            set_clauses.append("fetch_method = %s")
            params.append(fetch_method)
        if api_provider is not None:
            set_clauses.append("api_provider = %s")
            params.append(api_provider)
        if base_url is not None:
            set_clauses.append("base_url = %s")
            params.append(base_url)

        if not set_clauses:
            cur.execute("select id from price_sites where id = %s", (site_id,))
            if cur.fetchone() is None:
                raise LookupError(f"No price_sites row with id {site_id}")
            conn.commit()
            return {"id": site_id}

        params.append(site_id)
        cur.execute(
            f"update price_sites set {', '.join(set_clauses)} where id = %s returning id",
            params,
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No price_sites row with id {site_id}")
    conn.commit()
    return {"id": site_id}


def delete_price_site(conn, site_id: str) -> dict:
    """Hard delete -- cascades to every product_price_sources row (and
    THEIR product_price_history rows) pointed at this site, per migration
    014's `on delete cascade`. Real, deliberate difference from
    delete_video_candidate's docstring reasoning (which warns hard delete
    can let a row silently resurface elsewhere): there's no discovery
    process that could re-create a price_sites row on its own, so no
    tombstone/re-creation risk here -- unlike a video candidate, nothing
    will ever re-insert a deleted site behind an admin's back."""
    with conn.cursor() as cur:
        cur.execute("delete from price_sites where id = %s returning id", (site_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No price_sites row with id {site_id}")
    conn.commit()
    return {"deleted": True, "id": site_id}


def list_product_price_sources(conn, product_id: str, status: str = None) -> list:
    """This product's "site setup" -- price_sites rows discovery has
    matched (or an admin has manually attached) to it, plus the SITE's
    name (for display) and its latest history row (price/checked_at/
    error) as a convenience, via a correlated subquery -- same live-
    computed-not-stored pattern public_api/admin_api's popularity_score
    subquery already uses (see that section's own comment in
    list_products), picked here for the same reason: "latest price" is
    inherently derived from product_price_history, not a fact worth
    duplicating onto product_price_sources itself.

    status=None (the product-detail view's default, mirroring
    list_video_candidates' own status=None case) returns every status --
    pending/approved/rejected together -- so an admin reviewing one
    product's price tracking can see a rejected mismatch sitting next to
    the approved source that replaced it, not just whichever one
    happens to be active right now.

    fetch_method (ps.fetch_method) plus latest_cost_price/latest_in_stock
    (016_price_tracking_bigcommerce.sql, same correlated-subquery pattern
    as latest_price/latest_checked_at/latest_error) let the admin-site
    show BowlerDepot's cost/stock data next to its price without a second
    call -- both are simply null for a scrape-sourced row, same as
    latest_price is null for a source that's never been checked yet.

    base_url (ps.base_url) is also included -- Al: "the href in the admin
    ui on the price sources page is relative so it is broken... it needs
    to be fully qualified for the site it is for." pps.product_url is
    SUPPOSED to already be an absolute URL by the time it's stored (see
    extract_bigcommerce_price_fields/parse_search_results, both resolve
    relative hrefs via urljoin before insert), but a price_sites row
    created without its own base_url filled in, or a manually-added
    product_url pasted without a scheme, can still land here relative --
    exposing the site's base_url lets the admin-site resolve either case
    defensively at render time instead of trusting product_url is always
    already absolute."""
    query = """
        select
            pps.id, pps.price_site_id, ps.name as site_name, ps.fetch_method, pps.product_url,
            coalesce(pps.css_selector, ps.default_css_selector) as css_selector,
            pps.match_query, pps.match_confidence, pps.status, pps.source,
            pps.is_active, pps.last_checked_at, pps.created_at, pps.resolved_at, pps.resolved_by,
            (select h.price from product_price_history h
             where h.price_source_id = pps.id order by h.checked_at desc limit 1) as latest_price,
            (select h.checked_at from product_price_history h
             where h.price_source_id = pps.id order by h.checked_at desc limit 1) as latest_checked_at,
            (select h.error from product_price_history h
             where h.price_source_id = pps.id order by h.checked_at desc limit 1) as latest_error,
            (select h.cost_price from product_price_history h
             where h.price_source_id = pps.id order by h.checked_at desc limit 1) as latest_cost_price,
            (select h.in_stock from product_price_history h
             where h.price_source_id = pps.id order by h.checked_at desc limit 1) as latest_in_stock,
            ps.base_url
        from product_price_sources pps
        join price_sites ps on ps.id = pps.price_site_id
        where pps.product_id = %s
    """
    params = [product_id]
    if status is not None:
        query += " and pps.status = %s"
        params.append(status)
    query += " order by ps.name asc, pps.created_at asc, pps.id asc"

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [
        {
            "id": r[0], "price_site_id": r[1], "site_name": r[2], "fetch_method": r[3], "product_url": r[4],
            "css_selector": r[5], "match_query": r[6], "match_confidence": r[7],
            "status": r[8], "source": r[9], "is_active": r[10], "last_checked_at": r[11],
            "created_at": r[12], "resolved_at": r[13], "resolved_by": r[14],
            "latest_price": r[15], "latest_checked_at": r[16], "latest_error": r[17],
            "latest_cost_price": r[18], "latest_in_stock": r[19], "base_url": r[20],
        }
        for r in rows
    ]


def get_pending_price_source_count(conn) -> int:
    """Same shape as get_pending_video_count -- feeds an admin-site badge
    count for the Price Sources review queue."""
    with conn.cursor() as cur:
        cur.execute("select count(*) from product_price_sources where status = 'pending'")
        return cur.fetchone()[0]


def list_price_sources(conn, status: str = "pending", product_id: str = None, limit: int = 50, offset: int = 0) -> list:
    """Catalog-wide review queue -- mirrors list_video_candidates almost
    exactly (same status/product_id/limit/offset shape, same pv.id-style
    id tiebreaker for stable pagination -- see that function's own
    docstring for the real production bug that tiebreaker fixes, which
    applies here just as much: a single discovery invocation can insert
    many product_price_sources rows with near-identical created_at
    timestamps).

    ps.base_url is included for the same "resolve a relative product_url
    defensively at render time" reason list_product_price_sources' own
    base_url column exists for -- see that function's docstring."""
    query = """
        select pps.id, pps.product_id, p.name as product_name, b.name as brand_name,
               pps.price_site_id, ps.name as site_name, pps.product_url, ps.base_url,
               coalesce(pps.css_selector, ps.default_css_selector) as css_selector,
               pps.match_query, pps.match_confidence, pps.status, pps.source,
               pps.is_active, pps.last_checked_at,
               pps.created_at, pps.resolved_at, pps.resolved_by
        from product_price_sources pps
        join products p on p.id = pps.product_id
        join brands b on b.id = p.brand_id
        join price_sites ps on ps.id = pps.price_site_id
    """
    params = []
    conditions = []
    if status is not None:
        conditions.append("pps.status = %s")
        params.append(status)
    if product_id:
        conditions.append("pps.product_id = %s")
        params.append(product_id)
    if conditions:
        query += " where " + " and ".join(conditions)
    query += " order by pps.match_confidence asc, pps.created_at asc, pps.id asc limit %s offset %s"
    params += [limit, offset]

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def approve_price_source(conn, source_id: str, resolved_by: str) -> dict:
    """Marks the candidate approved -- from this point on, price_checker's
    checking job shape actually includes it (see list_price_sources_due/
    list_price_sources_for_products, both scoped to status='approved').
    Same one-way pending -> approved guard as approve_video_candidate,
    for the same reason: a bulk action or a stale UI double-click
    shouldn't silently re-apply a decision. See restore_price_source for
    the way back out."""
    with conn.cursor() as cur:
        cur.execute("select status from product_price_sources where id = %s", (source_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_price_sources row with id {source_id}")
        if row[0] != "pending":
            raise ValueError(f"product_price_sources row {source_id} is already {row[0]}, not pending")

        cur.execute(
            "update product_price_sources set status = 'approved', resolved_at = now(), resolved_by = %s where id = %s",
            (resolved_by, source_id),
        )
    conn.commit()
    return {"source_id": source_id, "status": "approved"}


def reject_price_source(conn, source_id: str, resolved_by: str, reason: str = None) -> dict:
    """Same shape/guard as reject_video_candidate. reason isn't persisted
    anywhere yet (product_price_sources has no reason column, mirroring
    product_videos' own lack of one) -- accepted here purely for call-
    site symmetry with reject_video_candidate/the admin-site's shared
    reject-with-reason UI, same as that function's own parameter."""
    with conn.cursor() as cur:
        cur.execute("select status from product_price_sources where id = %s", (source_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_price_sources row with id {source_id}")
        if row[0] != "pending":
            raise ValueError(f"product_price_sources row {source_id} is already {row[0]}, not pending")

        cur.execute(
            "update product_price_sources set status = 'rejected', resolved_at = now(), resolved_by = %s where id = %s",
            (resolved_by, source_id),
        )
    conn.commit()
    return {"source_id": source_id, "status": "rejected"}


def restore_price_source(conn, source_id: str) -> dict:
    """Undoes a mistaken approve/reject -- built in from the start here,
    unlike product_videos (where this only got added after Al hit the
    gap live: "it appears if i accidentally reject a video i can not
    undo that action"). Same behavior as restore_video_candidate: moves
    an already-resolved row (status IN ('approved', 'rejected')) back to
    'pending' and clears resolved_at/resolved_by. No resolved_by
    parameter, same reasoning as restore_video_candidate -- there's no
    decision to attribute when undoing one. Restoring an already-pending
    row is a hard error, not a silent no-op, same "stale UI state is
    worth surfacing" stance."""
    with conn.cursor() as cur:
        cur.execute("select status from product_price_sources where id = %s", (source_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_price_sources row with id {source_id}")
        if row[0] not in ("approved", "rejected"):
            raise ValueError(f"product_price_sources row {source_id} is {row[0]}, not approved or rejected -- nothing to restore")

        cur.execute(
            "update product_price_sources set status = 'pending', resolved_at = null, resolved_by = null where id = %s",
            (source_id,),
        )
    conn.commit()
    return {"source_id": source_id, "status": "pending"}


def create_product_price_source(conn, product_id: str, price_site_id: str, product_url: str,
                                 css_selector: str = None, resolved_by: str = None,
                                 external_product_id: str = None) -> dict:
    """The manual-override path -- Al: "admin can fix mismatches manually
    after the fact if a match is wrong." Not the primary way sources get
    created (that's price_checker's discovery job, see this section's own
    header comment) -- this is for when discovery didn't find a real
    match at all, or found the wrong one and an admin wants to attach the
    correct URL directly. Immediately status='approved', source='manual'
    -- there's no candidate to review here, an admin just told this
    system the exact URL directly, same trust level as approving a
    candidate by hand. Existence-checks both foreign keys up front (same
    reasoning as reassign_video_candidate's target-product check) so a
    bad id surfaces as a clear 404-shaped LookupError instead of an
    opaque IntegrityError from the FK constraint. css_selector is
    optional -- null means "use this site's default_css_selector" (see
    price_checker.list_price_sources_due's coalesce).

    external_product_id (016_price_tracking_bigcommerce.sql) is only
    meaningful for a manual override against an 'api'-fetch_method site
    (e.g. an admin manually attaching a BowlerDepot product this system's
    own discovery pass missed) -- optional and null by default, harmless
    for a 'scrape' site where price_checker's checking path never reads
    it."""
    with conn.cursor() as cur:
        cur.execute("select id from products where id = %s", (product_id,))
        if cur.fetchone() is None:
            raise LookupError(f"No product with id {product_id}")
        cur.execute("select id from price_sites where id = %s", (price_site_id,))
        if cur.fetchone() is None:
            raise LookupError(f"No price_sites row with id {price_site_id}")

        cur.execute(
            """
            insert into product_price_sources
                (product_id, price_site_id, product_url, css_selector, external_product_id,
                 status, source, resolved_at, resolved_by)
            values (%s, %s, %s, %s, %s, 'approved', 'manual', now(), %s)
            returning id
            """,
            (product_id, price_site_id, product_url, css_selector, external_product_id, resolved_by),
        )
        source_id = cur.fetchone()[0]
    conn.commit()
    return {"id": source_id, "product_id": product_id, "price_site_id": price_site_id,
            "product_url": product_url, "external_product_id": external_product_id,
            "status": "approved", "source": "manual"}


def update_product_price_source(conn, source_id: str, product_url: str = None,
                                 css_selector: str = None, is_active: bool = None) -> dict:
    """Partial update, same convention as update_price_site. is_active is
    how an admin pauses checking a source (e.g. a retailer stopped
    carrying this ball) without losing its price_price_history."""
    with conn.cursor() as cur:
        set_clauses = []
        params = []
        if product_url is not None:
            set_clauses.append("product_url = %s")
            params.append(product_url)
        if css_selector is not None:
            set_clauses.append("css_selector = %s")
            params.append(css_selector)
        if is_active is not None:
            set_clauses.append("is_active = %s")
            params.append(is_active)

        if not set_clauses:
            cur.execute("select id from product_price_sources where id = %s", (source_id,))
            if cur.fetchone() is None:
                raise LookupError(f"No product_price_sources row with id {source_id}")
            conn.commit()
            return {"id": source_id}

        params.append(source_id)
        cur.execute(
            f"update product_price_sources set {', '.join(set_clauses)} where id = %s returning id",
            params,
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_price_sources row with id {source_id}")
    conn.commit()
    return {"id": source_id}


def delete_product_price_source(conn, source_id: str) -> dict:
    """Hard delete -- cascades to this source's product_price_history
    rows too (migration 014's `on delete cascade`), same as
    delete_price_site. There's no video-candidate-style "could resurface
    on the next rescan" concern here either: price_checker never creates
    a product_price_sources row on its own, only admins do via
    create_product_price_source, so deleting one is final in the same
    uncomplicated way delete_price_site is."""
    with conn.cursor() as cur:
        cur.execute("delete from product_price_sources where id = %s returning id", (source_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_price_sources row with id {source_id}")
    conn.commit()
    return {"deleted": True, "id": source_id}


def _is_absolute_url(url: str) -> bool:
    """True when url has a scheme (http://, https://, etc) -- used below
    to prefer an already-resolved product_url over a stray relative one
    when merging duplicate rows. Same "has a scheme" definition as the
    admin-site's own resolveExternalUrl JS helper, just via urlparse
    instead of a regex since this runs server-side."""
    return bool(url) and bool(urlparse(url).scheme)


def dedupe_product_price_sources(conn) -> dict:
    """One-off cleanup for a real duplication bug found live. Al: "there
    are duplicates now, the ones before having the baseurl and now the
    ones that have it... same record just has different link."

    Root cause: extract_bigcommerce_price_fields (price_checker/app.py)
    falls back to the raw relative custom_url when a price_sites row's
    base_url isn't configured, so a product_price_sources row discovered
    before base_url was filled in got a relative product_url.
    insert_price_source_candidates' ON CONFLICT DO NOTHING is keyed on
    the literal (product_id, price_site_id, product_url) triple (014_
    price_tracking.sql) -- once base_url got filled in, re-running
    discovery computed a different (absolute) product_url for the exact
    same real-world product+site pair, so the conflict target didn't
    match and a second row got INSERTed instead of the first one being
    corrected in place. price_checker.upsert_bigcommerce_price_source_
    candidate is the matching root-cause fix that stops this from
    recurring going forward -- this function only cleans up rows that
    already exist from before that fix shipped.

    For every (product_id, price_site_id) pair with more than one row:
    picks a single survivor -- approved+active first (that's the row any
    real price/stock history would have accumulated on, since price_
    checker only ever checks approved+active rows), else the oldest row
    -- reassigns every other row's product_price_history and product_sku_
    stock_history rows onto the survivor first (both tables' price_
    source_id is `on delete cascade`, so deleting a redundant row without
    this step would silently discard any history it happened to carry),
    then deletes every non-survivor row in the group. Finally, if any row
    in the group has a strictly more resolved product_url (absolute where
    the survivor's own is still relative -- see _is_absolute_url) than
    the survivor's current one, updates the survivor to that better
    value -- covers the common case where the OLD, history-bearing row is
    the one stuck with the stale relative URL and the freshly-discovered
    duplicate is the one with the correct absolute link.

    Idempotent and safe to re-run: a catalog with no duplicate groups left
    just returns groups_merged=0 rows_deleted=0, doing nothing."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, product_id, price_site_id, product_url, status, is_active, created_at
            from product_price_sources
            order by product_id, price_site_id, created_at asc
            """
        )
        rows = cur.fetchall()

    groups = {}
    for r in rows:
        key = (r[1], r[2])
        groups.setdefault(key, []).append(
            {"id": r[0], "product_url": r[3], "status": r[4], "is_active": r[5], "created_at": r[6]}
        )

    groups_merged = 0
    rows_deleted = 0
    with conn.cursor() as cur:
        for group_rows in groups.values():
            if len(group_rows) < 2:
                continue
            groups_merged += 1

            group_rows.sort(key=lambda row: (0 if (row["status"] == "approved" and row["is_active"]) else 1, row["created_at"]))
            survivor = group_rows[0]
            others = group_rows[1:]

            best_url = survivor["product_url"]
            for row in others:
                if row["product_url"] and _is_absolute_url(row["product_url"]) and not _is_absolute_url(best_url):
                    best_url = row["product_url"]

            for row in others:
                cur.execute(
                    "update product_price_history set price_source_id = %s where price_source_id = %s",
                    (survivor["id"], row["id"]),
                )
                cur.execute(
                    "update product_sku_stock_history set price_source_id = %s where price_source_id = %s",
                    (survivor["id"], row["id"]),
                )
                cur.execute("delete from product_price_sources where id = %s", (row["id"],))
                rows_deleted += 1

            if best_url != survivor["product_url"]:
                cur.execute(
                    "update product_price_sources set product_url = %s where id = %s",
                    (best_url, survivor["id"]),
                )

    conn.commit()
    return {"groups_merged": groups_merged, "rows_deleted": rows_deleted}


def get_price_history(conn, product_id: str, days: int = 90) -> dict:
    """Read side for the actual "chart price over time" ask -- returns
    both this product's configured sources (for a legend/label lookup)
    and the raw history rows within the trailing `days` window, across
    ALL of this product's sources at once so the admin UI can draw one
    line per source on a single chart without N separate calls. Rows
    with error IS NOT NULL are still included (not filtered out) --
    same "a failed check is still visible" stance product_price_history
    itself takes (see migration 014's header comment); it's the chart-
    rendering layer's job to decide how to draw a gap or a marker for
    those, not this query's job to hide them.

    cost_price/in_stock (016_price_tracking_bigcommerce.sql) ride along
    in the same history rows -- null for every scrape-sourced check, real
    values for a BowlerDepot/'api' check -- so a caller building a
    BowlerDepot-specific cost/stock-over-time view doesn't need a second
    query against this same table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select pps.id, ps.name as site_name
            from product_price_sources pps
            join price_sites ps on ps.id = pps.price_site_id
            where pps.product_id = %s and pps.status = 'approved'
            order by ps.name asc
            """,
            (product_id,),
        )
        sources = [{"id": r[0], "site_name": r[1]} for r in cur.fetchall()]

        cur.execute(
            """
            select h.price_source_id, h.price, h.error, h.checked_at, h.cost_price, h.in_stock
            from product_price_history h
            join product_price_sources pps on pps.id = h.price_source_id
            where pps.product_id = %s
              and h.checked_at >= now() - (%s || ' days')::interval
            order by h.checked_at asc
            """,
            (product_id, days),
        )
        history = [
            {
                "price_source_id": r[0], "price": r[1], "error": r[2], "checked_at": r[3],
                "cost_price": r[4], "in_stock": r[5],
            }
            for r in cur.fetchall()
        ]

    return {"sources": sources, "history": history}


def get_sku_stock_history(conn, product_id: str, days: int = 90) -> dict:
    """Read side of 017_price_tracking_sku_stock.sql -- Al: "for the
    instock i was refering to actual number of each sku instock... track
    how many are being sold and when are they restocked." Same two-query
    shape as get_price_history immediately above: first this product's own
    SKUs (for a legend/label lookup, one row per weight), then the raw
    quantity readings within the trailing `days` window across ALL of this
    product's SKUs at once, so the admin UI can draw one line per weight
    on a single chart without N separate calls.

    "How many sold / when restocked" (Al's own framing) is intentionally
    NOT computed here -- this returns the raw readings in checked_at order
    and leaves the day-over-day delta (a drop is sold-since-last-check, a
    rise is a restock) to the caller/chart layer, same live-computed-not-
    stored posture this project already takes for popularity_score/
    latest_price elsewhere; see 017's own migration header comment for the
    full reasoning and its "can't fully distinguish 12 sold/0 restocked
    from 2 sold/10 restocked on the same day" honesty note.

    quantity rides through as-is, including null (BigCommerce not
    tracking that variant's inventory that check) -- never coerced to 0;
    see product_sku_stock_history.quantity's own column comment."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, weight_lbs
            from product_skus
            where product_id = %s
            order by weight_lbs asc
            """,
            (product_id,),
        )
        skus = [{"id": r[0], "weight_lbs": r[1]} for r in cur.fetchall()]

        cur.execute(
            """
            select h.product_sku_id, h.price_source_id, h.quantity, h.checked_at
            from product_sku_stock_history h
            join product_skus sk on sk.id = h.product_sku_id
            where sk.product_id = %s
              and h.checked_at >= now() - (%s || ' days')::interval
            order by h.checked_at asc
            """,
            (product_id, days),
        )
        history = [
            {"product_sku_id": r[0], "price_source_id": r[1], "quantity": r[2], "checked_at": r[3]}
            for r in cur.fetchall()
        ]

    return {"skus": skus, "history": history}


def queue_price_check(conn, product_id: str) -> dict:
    """On-demand "check price now" trigger for one product's configured
    sources -- same shape as queue_video_discovery immediately above
    (direct lambda:InvokeFunction, async/fire-and-forget, same
    {"queued": False, "reason": ...} soft-fail convention when
    PRICE_CHECKER_FUNCTION_NAME isn't configured on this deployment).
    price_checker's own {"product_ids": [...]} job shape (see its module
    docstring) is what actually scopes the check to just this product's
    active sources."""
    with conn.cursor() as cur:
        cur.execute("select id from products where id = %s", (product_id,))
        if cur.fetchone() is None:
            raise LookupError(f"No product with id {product_id}")

    function_name = os.environ.get("PRICE_CHECKER_FUNCTION_NAME")
    if not function_name:
        return {"queued": False, "reason": "PRICE_CHECKER_FUNCTION_NAME is not configured on this deployment"}

    import boto3

    lambda_client = boto3.client("lambda")
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps({"product_ids": [product_id]}),
    )
    return {"queued": True, "product_id": product_id}


def queue_price_check_batch(limit: int = None) -> dict:
    """Catalog-wide "check prices now" trigger -- same shape as
    queue_video_stats_refresh immediately above (no conn/existence check,
    since there's no single row whose absence would 404; limit=None lets
    price_checker fall back to its own DEFAULT_PRICE_CHECK_LIMIT rather
    than this layer needing to know that number too)."""
    function_name = os.environ.get("PRICE_CHECKER_FUNCTION_NAME")
    if not function_name:
        return {"queued": False, "reason": "PRICE_CHECKER_FUNCTION_NAME is not configured on this deployment"}

    import boto3

    payload = {}
    if limit is not None:
        payload["limit"] = limit

    lambda_client = boto3.client("lambda")
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload),
    )
    return {"queued": True, "limit": limit}


def queue_price_discovery(conn, product_id: str) -> dict:
    """On-demand "search for price sources" trigger for one product --
    same shape as queue_video_discovery, just invoking PriceCheckerFunction
    with a {"discover": true, "product_ids": [...]} job instead of
    VideoDiscoveryFunction's own scope shape (see price_checker.app's
    module docstring for the discovery job's own shapes). This is the
    thing a product-detail "find price sources" button (mirroring the
    existing Videos section's rescan button) calls."""
    with conn.cursor() as cur:
        cur.execute("select id from products where id = %s", (product_id,))
        if cur.fetchone() is None:
            raise LookupError(f"No product with id {product_id}")

    function_name = os.environ.get("PRICE_CHECKER_FUNCTION_NAME")
    if not function_name:
        return {"queued": False, "reason": "PRICE_CHECKER_FUNCTION_NAME is not configured on this deployment"}

    import boto3

    lambda_client = boto3.client("lambda")
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps({"discover": True, "product_ids": [product_id]}),
    )
    return {"queued": True, "product_id": product_id}


def queue_price_discovery_batch(limit: int = None, scrape_only: bool = False) -> dict:
    """Catalog-wide "search for price sources" trigger -- same shape as
    queue_video_stats_refresh/queue_price_check_batch (no conn/existence
    check; limit=None lets price_checker fall back to its own
    DEFAULT_MAX_PRODUCTS_PER_DISCOVERY_INVOCATION).

    scrape_only=True passes {"scrape_only": true} straight through to
    price_checker.discover_price_sources, skipping every 'api' fetch_
    method site (BowlerDepot) entirely for this run -- see that
    function's own docstring for why (Al: "can we not run the bowlerdepot
    price sources in this one, they have inventory numbers too")."""
    function_name = os.environ.get("PRICE_CHECKER_FUNCTION_NAME")
    if not function_name:
        return {"queued": False, "reason": "PRICE_CHECKER_FUNCTION_NAME is not configured on this deployment"}

    import boto3

    payload = {"discover": True}
    if limit is not None:
        payload["limit"] = limit
    if scrape_only:
        payload["scrape_only"] = True

    lambda_client = boto3.client("lambda")
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload),
    )
    return {"queued": True, "limit": limit, "scrape_only": scrape_only}


# ---------------------------------------------------------------------
# Blocked video channels (021_blocked_video_channels.sql) -- Al: "i feel
# like a filter is probably necessary because some of these videos are
# from our competitors and we should avoid putting those on there."
# A simple admin-curated blocklist by channel display name (no stable
# channel_id is captured anywhere in this pipeline -- see the migration's
# own header comment), consumed only by src/bowlerdepot_video_sync's
# list_videos_needing_sync query to keep a competitor's actual video off
# BowlerDepot's product pages. Deliberately does NOT touch video
# discovery, approval, or the video_reviews_summary rollup -- Al
# explicitly wants the rollup to keep drawing on every approved video's
# summary regardless of channel ("I would like to include the summary of
# summaries even if it is built off of one of theirs").
# ---------------------------------------------------------------------

def list_blocked_channels(conn) -> list:
    """Every blocked channel, most-recently-added first -- the small
    admin-site panel just needs a flat list to render with a delete
    button per row, same shape as list_price_sites but with no
    active/inactive state (a row here IS the block; removing it is just
    a delete, see delete_blocked_channel)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, channel_title, note, created_at
            from blocked_video_channels
            order by created_at desc
            """
        )
        rows = cur.fetchall()
    return [
        {"id": r[0], "channel_title": r[1], "note": r[2], "created_at": r[3]}
        for r in rows
    ]


def create_blocked_channel(conn, channel_title: str, note: str = None) -> dict:
    """Adds one channel to the blocklist. `on conflict ... do nothing`
    against the case-insensitive unique index (021_blocked_video_
    channels.sql) means re-blocking an already-blocked channel (even
    with different casing) is a harmless no-op rather than an
    IntegrityError -- same "let the DB constraint be the source of
    truth, but don't make the caller pre-check" posture as
    insert_price_source_candidates' own on-conflict-do-nothing insert."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into blocked_video_channels (channel_title, note)
            values (%s, %s)
            on conflict (lower(channel_title)) do nothing
            returning id, channel_title, note, created_at
            """,
            (channel_title, note),
        )
        row = cur.fetchone()
        if row is None:
            # Already blocked (case-insensitive match) -- look up the
            # existing row so the caller still gets a real id back
            # rather than None, same "return the row that actually
            # exists" courtesy as any other dedupe-on-conflict path.
            cur.execute(
                "select id, channel_title, note, created_at from blocked_video_channels where lower(channel_title) = lower(%s)",
                (channel_title,),
            )
            row = cur.fetchone()
    conn.commit()
    return {"id": row[0], "channel_title": row[1], "note": row[2], "created_at": row[3]}


def delete_blocked_channel(conn, channel_id: str) -> dict:
    """Un-blocks a channel -- hard delete, same reasoning as
    delete_price_site: nothing else in this pipeline will ever re-create
    a blocked_video_channels row on its own, so there's no
    tombstone/resurface risk to guard against."""
    with conn.cursor() as cur:
        cur.execute("delete from blocked_video_channels where id = %s returning id", (channel_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No blocked_video_channels row with id {channel_id}")
    conn.commit()
    return {"deleted": True, "id": channel_id}


# --- Manual seed URLs (orphan-page catch, 027_manual_seed_urls.sql) ----------
#
# REAL INCIDENT (2026-09-04): Al reported storm-equinox-bowling-ball
# missing from the site. Root-caused as a genuine orphan page (live,
# in-stock, but linked from neither commercebuild_url_discovery's
# category-listing crawl nor its sitemap fetch -- stormbowling.com itself
# stopped linking to it internally). A one-off manual Lambda invocation
# fixed that one product; this table + these three functions are the
# permanent catch for the next page like it. See commercebuild_url_
# discovery/app.py's discover_manual_seed_urls for the consuming side.

def list_manual_seed_urls(conn) -> list:
    """Every seed URL, most-recently-added first, joined to the brand's
    own name so the admin-site panel can show something more useful than
    a raw brand_id -- same "join for display, don't make the frontend do
    a second lookup" posture as get_product's core/coverstock joins."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select m.id, m.brand_id, b.name as brand_name, m.url, m.note, m.created_at
            from manual_seed_urls m
            join brands b on b.id = m.brand_id
            order by m.created_at desc
            """
        )
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def create_manual_seed_url(conn, brand_id: str, url: str, note: str = None) -> dict:
    """Adds one seed URL. `on conflict (url) do nothing` (plain unique
    constraint, NOT case-insensitive like blocked_video_channels -- see
    027_manual_seed_urls.sql's header comment on why URLs are treated
    differently from free-text channel names) means re-seeding an
    already-seeded URL is a harmless no-op, same dedupe-on-conflict
    posture as create_blocked_channel."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into manual_seed_urls (brand_id, url, note)
            values (%s, %s, %s)
            on conflict (url) do nothing
            returning id, brand_id, url, note, created_at
            """,
            (brand_id, url, note),
        )
        row = cur.fetchone()
        if row is None:
            # Already seeded -- look up the existing row so the caller
            # still gets a real id back rather than None, same courtesy
            # as create_blocked_channel's own conflict path.
            cur.execute(
                "select id, brand_id, url, note, created_at from manual_seed_urls where url = %s",
                (url,),
            )
            row = cur.fetchone()
    conn.commit()
    return {"id": row[0], "brand_id": row[1], "url": row[2], "note": row[3], "created_at": row[4]}


def delete_manual_seed_url(conn, seed_id: str) -> dict:
    """Removes a seed -- hard delete. Does NOT touch discovered_urls or
    any product/SKU data already scraped from that URL if it was already
    picked up by a discovery run; this only stops it from being
    RE-seeded/re-emphasized going forward, same "delete the intent, not
    the downstream effect" reasoning as delete_blocked_channel."""
    with conn.cursor() as cur:
        cur.execute("delete from manual_seed_urls where id = %s returning id", (seed_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No manual_seed_urls row with id {seed_id}")
    conn.commit()
    return {"deleted": True, "id": seed_id}


# --- Ball-review-article generation (022_product_articles.sql) --------------
#
# Al: "Do you think we generate ball review article like the one here:
# [bowling.com's Storm Equinox Hybrid review]... this could be the backend
# that pulls together all the creative and content for the frontend." Scoped
# via a follow-up AskUserQuestion exchange: full bowling.com-shaped article
# (not just the FAQ piece), and -- same as product_videos/product_price_
# sources -- generated content sits in product_articles.status
# (pending/approved/rejected) and is never auto-exposed to public_api until
# an admin approves it here. list_articles/get_article/approve_article/
# reject_article below intentionally mirror list_price_sources/
# get_video_candidate/approve_price_source/reject_price_source's exact
# shapes (see those functions above) rather than inventing a new pattern.

def list_articles(conn, status: str = "pending", product_id: str = None, limit: int = 50, offset: int = 0) -> list:
    """Lists product_articles rows joined with product/brand name for
    display, same join-for-display shape as list_price_sources. status=None
    (the admin-site "all" option, same convention as
    GET /video-candidates?status=all) returns every status.

    sync_to_bigcommerce/bigcommerce_post_id/bowlerdepot_synced_at
    (028_product_articles_bigcommerce_sync.sql) are included so the
    Articles tab list row can show/toggle the sync flag without a second
    round-trip per row -- same "columns the list view needs come along in
    this same select" reasoning the action_shot_image_url/
    images_generated_at columns above already follow."""
    query = """
        select pa.id, pa.product_id, p.name as product_name, b.name as brand_name,
               pa.status, pa.title, pa.generated_at, pa.reviewed_at,
               pa.resolved_by, pa.created_at,
               pa.action_shot_image_url, pa.product_shot_image_url, pa.images_generated_at,
               pa.sync_to_bigcommerce, pa.bigcommerce_post_id, pa.bowlerdepot_synced_at
        from product_articles pa
        join products p on p.id = pa.product_id
        join brands b on b.id = p.brand_id
    """
    params = []
    conditions = []
    if status is not None:
        conditions.append("pa.status = %s")
        params.append(status)
    if product_id:
        conditions.append("pa.product_id = %s")
        params.append(product_id)
    if conditions:
        query += " where " + " and ".join(conditions)
    query += " order by pa.created_at desc, pa.id asc limit %s offset %s"
    params += [limit, offset]

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_article(conn, article_id: str):
    """Full detail for one article, including all the generated content
    fields (title/hook/performance_summary/faq/etc) -- the admin-site
    review view renders straight off this. Returns None if not found
    (same "let the route layer turn None into a 404" convention as
    get_video_candidate) rather than raising."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select pa.*, p.name as product_name, b.name as brand_name
            from product_articles pa
            join products p on p.id = pa.product_id
            join brands b on b.id = p.brand_id
            where pa.id = %s
            """,
            (article_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cur.description]
        return dict(zip(columns, row))


def approve_article(conn, article_id: str, resolved_by: str) -> dict:
    """Mirrors approve_price_source: only a pending article can be
    approved (fails closed with ValueError on an already-resolved row
    rather than silently re-stamping reviewed_at/resolved_by)."""
    with conn.cursor() as cur:
        cur.execute("select status from product_articles where id = %s", (article_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_articles row with id {article_id}")
        if row[0] != "pending":
            raise ValueError(f"product_articles row {article_id} is already {row[0]}, not pending")

        cur.execute(
            "update product_articles set status = 'approved', reviewed_at = now(), resolved_by = %s where id = %s",
            (resolved_by, article_id),
        )
    conn.commit()
    return {"article_id": article_id, "status": "approved"}


def reject_article(conn, article_id: str, resolved_by: str, reason: str = None) -> dict:
    """Mirrors reject_price_source. `reason` is accepted for call-site/
    admin-site symmetry with the video-candidate and price-source reject
    routes but, same as those, isn't persisted -- there's no reason
    column on product_articles."""
    with conn.cursor() as cur:
        cur.execute("select status from product_articles where id = %s", (article_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_articles row with id {article_id}")
        if row[0] != "pending":
            raise ValueError(f"product_articles row {article_id} is already {row[0]}, not pending")

        cur.execute(
            "update product_articles set status = 'rejected', reviewed_at = now(), resolved_by = %s where id = %s",
            (resolved_by, article_id),
        )
    conn.commit()
    return {"article_id": article_id, "status": "rejected"}


def set_article_bigcommerce_sync(conn, article_id: str, sync_to_bigcommerce: bool) -> dict:
    """Toggles product_articles.sync_to_bigcommerce (028_product_articles_
    bigcommerce_sync.sql) -- Al: "lets add a flag to each article that
    would sync them to bigcommerce if on." Mirrors set_product_published's
    exact shape (single-column update, LookupError if the row doesn't
    exist, no pending/approved status gate) rather than approve_article/
    reject_article's review-workflow shape: this is a lightweight,
    freely-reversible admin preference, not a one-way resolution of a
    review-queue item -- an admin can flip it on and off as many times as
    they want, on an article of any status.

    Deliberately does NOT touch bigcommerce_post_id/bowlerdepot_synced_at
    -- those are the sync job's own bookkeeping (set only when it
    actually pushes something), never written by this toggle itself. See
    that migration's header comment for what turning this off after a
    successful sync does and doesn't mean."""
    with conn.cursor() as cur:
        cur.execute(
            "update product_articles set sync_to_bigcommerce = %s where id = %s returning id",
            (sync_to_bigcommerce, article_id),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_articles row with id {article_id}")
    conn.commit()
    return {"article_id": article_id, "sync_to_bigcommerce": sync_to_bigcommerce}


def queue_article_sync(conn, article_id: str) -> dict:
    """On-demand "sync this article to BigCommerce now" trigger (POST
    /articles/{id}/sync-to-bigcommerce, the Articles tab's "Sync now"
    button) -- Al flipped sync_to_bigcommerce on for a real article via
    curl, got a clean 200, and reported "i don't see it in bigcommerce":
    correct, since at that point the flag was the only thing built and
    nothing actually pushes on its own except BowlerdepotArticleSyncFunc-
    tion's hourly schedule. This gives an immediate path instead of
    making an admin wait for the next tick.

    Direct lambda:InvokeFunction, no queue in front -- same convention as
    queue_video_discovery/queue_article_generation above. Payload is
    {"article_id": article_id}; bowlerdepot_article_sync/app.py's handler
    checks event.get("article_id") to scope to exactly this one row
    instead of scanning every outstanding article (see that module's own
    docstring for the two invocation shapes).

    Deliberately does NOT re-check sync_to_bigcommerce/status/match_status
    here -- those are BowlerdepotArticleSyncFunction's own needs-sync
    query's job (list_articles_needing_sync), same "the invoker doesn't
    duplicate the invokee's own gating logic" reasoning queue_video_
    discovery/queue_article_generation already follow. Invoking this for
    an article that isn't actually eligible (flag off, not approved,
    already synced) is harmless -- the sync job's own query just won't
    select it, so it silently no-ops rather than erroring."""
    with conn.cursor() as cur:
        cur.execute("select id from product_articles where id = %s", (article_id,))
        if cur.fetchone() is None:
            raise LookupError(f"No product_articles row with id {article_id}")

    function_name = os.environ.get("ARTICLE_SYNC_FUNCTION_NAME")
    if not function_name:
        return {"queued": False, "reason": "ARTICLE_SYNC_FUNCTION_NAME is not configured on this deployment"}

    import boto3

    lambda_client = boto3.client("lambda")
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps({"article_id": article_id}),
    )
    return {"queued": True, "article_id": article_id}


def queue_article_resync(conn, article_id: str) -> dict:
    """On-demand "re-push this ALREADY-synced article's current content
    onto BigCommerce" trigger (POST /articles/{id}/resync-to-bigcommerce,
    the Articles tab's "Resync" button) -- added right after the WebDAV
    Digest-auth fix made thumbnail_path actually start working: articles
    synced before that fix have no thumbnail, and simply re-flagging them
    does nothing since they're already synced (queue_article_sync's own
    invokee-side query -- list_articles_needing_sync -- explicitly
    excludes anything with bowlerdepot_synced_at already set). This is
    the fix: same direct lambda:InvokeFunction convention, but the
    payload also carries `"resync": true`, which routes bowlerdepot_
    article_sync/app.py's handler to list_articles_needing_resync and
    _process_one_article's update (PUT) branch instead of create (POST)
    -- overwrites the SAME existing BigCommerce post rather than creating
    a duplicate. See that module's own docstring for the full invocation-
    shape reasoning.

    Same existence-only check as queue_article_sync -- does NOT
    re-check sync_to_bigcommerce/bowlerdepot_synced_at/match_status here;
    those are list_articles_needing_resync's own job. Invoking this for
    an article that was never actually synced (no bigcommerce_post_id
    yet) is harmless -- the resync query just won't select it, so it
    silently no-ops rather than erroring or creating a duplicate."""
    with conn.cursor() as cur:
        cur.execute("select id from product_articles where id = %s", (article_id,))
        if cur.fetchone() is None:
            raise LookupError(f"No product_articles row with id {article_id}")

    function_name = os.environ.get("ARTICLE_SYNC_FUNCTION_NAME")
    if not function_name:
        return {"queued": False, "reason": "ARTICLE_SYNC_FUNCTION_NAME is not configured on this deployment"}

    import boto3

    lambda_client = boto3.client("lambda")
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps({"article_id": article_id, "resync": True}),
    )
    return {"queued": True, "article_id": article_id, "resync": True}


def list_article_image_candidates(conn, article_id: str) -> list:
    """Every image candidate 026_product_article_image_candidates.sql has
    ever stored for this article (both variants, both providers -- Gemini
    and Stability -- together, not split by variant), for the Articles
    review tab's candidate picker (see that migration's own header
    comment for why this is a separate table rather than more columns on
    product_articles: these rows persist EVERY candidate generated, not
    just the one currently live). Ordered by variant then created_at so
    the admin-site can group action_shot/product_shot into two rows of
    thumbnails without re-sorting client-side. Returns [] (not an error)
    for an article with no image candidates -- either images were never
    configured on this deployment (see generate_article_for_product's own
    gating condition) or generation failed for both variants that run,
    same "images are always best-effort" posture as the rest of this
    feature."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, article_id, variant, model_id, image_key, image_url, seed, is_selected, created_at
            from product_article_image_candidates
            where article_id = %s
            order by variant, created_at
            """,
            (article_id,),
        )
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def select_article_image_candidate(conn, candidate_id: str, resolved_by: str = None) -> dict:
    """Switches which candidate is "live" for its (article_id, variant) --
    a lightweight admin action, not a review/approve workflow of its own
    (same precedent as reorder_product_images, see that function's own
    docstring), which is why this takes just a candidate_id/resolved_by
    rather than mirroring approve_article/reject_article's pending-status
    gating. Two things happen in the same transaction: (1) the candidates
    table's own is_selected flag flips -- every other candidate for this
    (article_id, variant) pair is set to false, this one to true, so
    product_article_image_candidates_one_selected_idx's partial unique
    index (026_product_article_image_candidates.sql) never sees two
    selected rows even momentarily; (2) product_articles' own
    action_shot_image_key/url or product_shot_image_key/url (whichever
    variant this candidate belongs to) is overwritten to mirror the new
    selection, since public_api and every other part of this codebase
    that renders article images still reads those four flat columns, not
    this table, directly (see 026's own header comment for that design
    rationale). resolved_by is accepted and stored nowhere (there's no
    audit column on this table for it, and product_articles' own
    resolved_by/reviewed_at are the ARTICLE's review status, not per-
    image) -- kept as a parameter purely for call-site symmetry with
    reassign_video_candidate/approve_article, which do use theirs, and in
    case a future audit column is added here later."""
    with conn.cursor() as cur:
        cur.execute(
            "select article_id, variant, image_key, image_url from product_article_image_candidates where id = %s",
            (candidate_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_article_image_candidates row with id {candidate_id}")
        article_id, variant, image_key, image_url = row

        cur.execute(
            "update product_article_image_candidates set is_selected = false where article_id = %s and variant = %s",
            (article_id, variant),
        )
        cur.execute(
            "update product_article_image_candidates set is_selected = true where id = %s",
            (candidate_id,),
        )

        key_column = f"{variant}_image_key"
        url_column = f"{variant}_image_url"
        cur.execute(
            f"update product_articles set {key_column} = %s, {url_column} = %s where id = %s",
            (image_key, image_url, article_id),
        )
    conn.commit()
    return {"candidate_id": candidate_id, "article_id": article_id, "variant": variant,
            "image_key": image_key, "image_url": image_url}


def queue_article_generation(conn, product_id: str, mode: str = "both") -> dict:
    """Direct lambda:InvokeFunction, no queue in front -- same convention
    as queue_video_discovery, invoked from POST /products/{id}/generate-
    article (both the original "Generate article" button in product
    detail view and the original combined "Regenerate" button on an
    already-reviewed article) and, as of v7, also from the two decoupled
    routes POST /products/{id}/regenerate-article-text and .../
    regenerate-article-images (Al: "can we decouple the article and
    image regenerate").

    Payload key is deliberately `product_id` (singular), NOT `product_ids`
    (a list) like queue_video_discovery's VideoDiscoveryFunction payload --
    product_article_generator.app.handler's on-demand path checks
    event.get("product_id") specifically (see app.py's `if event.get(
    "product_id"):` branch); sending product_ids here would silently miss
    that branch and fall through to the catalog-wide
    list_products_needing_article scan instead of generating for just this
    product.

    v7 (2026-09-05): mode is "both" (default), "text", or "images".
    mode="both" is byte-for-byte the original behavior -- the payload
    doesn't even include the two new keys, so it's identical to every
    invocation this function sent before this change existed (handler()'s
    own regenerate_text/regenerate_images each default to True when
    absent from the event, so this is not just "usually the same", it's
    literally the same payload). mode="text"/"images" set regenerate_
    text/regenerate_images explicitly so handler() threads the right
    combination through to generate_article_for_product (see that
    function's own v7 docstring for what each combination actually does,
    e.g. why "images" requires an article to already exist)."""
    if mode not in ("both", "text", "images"):
        raise ValueError(f"Unknown mode {mode!r} -- expected 'both', 'text', or 'images'")

    with conn.cursor() as cur:
        cur.execute("select id from products where id = %s", (product_id,))
        if cur.fetchone() is None:
            raise LookupError(f"No product with id {product_id}")

    function_name = os.environ.get("PRODUCT_ARTICLE_GENERATOR_FUNCTION_NAME")
    if not function_name:
        return {"queued": False, "reason": "PRODUCT_ARTICLE_GENERATOR_FUNCTION_NAME is not configured on this deployment"}

    payload = {"product_id": product_id}
    if mode == "text":
        payload["regenerate_text"] = True
        payload["regenerate_images"] = False
    elif mode == "images":
        payload["regenerate_text"] = False
        payload["regenerate_images"] = True

    import boto3

    lambda_client = boto3.client("lambda")
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload),
    )
    return {"queued": True, "product_id": product_id, "mode": mode}


# --- User management (Cognito) ---
#
# Al: "can we add user management and a user group that has no access to
# user managment" -- built on the two Cognito groups AdminUserPool
# already ships with (see template.yaml's own AdminUserPoolAdminsGroup/
# AdminUserPoolEditorsGroup comment) rather than inventing a third:
# Admins get full CRUD over user accounts below, Editors get NONE of it
# -- see require_admin_role, the actual enforcement. This is the first
# route-level role gate this project has shipped: task #468 wired
# caller identity through admin_api but deliberately deferred gating any
# route on it ("per-route role enforcement is explicitly deferred future
# work" -- see resolve_caller_from_event's own docstring above). This is
# that deferred work, scoped narrowly to the one new surface that
# actually needs it (managing who else gets Admin/Editor access is
# exactly the kind of action Editors shouldn't have), not retrofitted
# onto every existing route in one pass.
#
# Unlike queue_rescrape/queue_video_discovery above (each builds its own
# boto3 client inline, one AWS call per function), the functions below
# take an already-built cognito_client as their first argument instead
# -- a deliberate departure from this file's usual "import boto3 inside
# the function" convention, because several of these (create_user,
# set_user_group, delete_user) make 2-3 SEQUENTIAL Cognito calls against
# the same client, and app.py's routes need to build exactly one
# cognito-idp client per request regardless of which of these functions
# it ends up calling. See app.py's _cognito_client() helper for where
# that single client gets constructed (same deferred "import boto3"
# placement, just one level up). Untested against a real Cognito user
# pool for the same reason every other AWS-touching function in this
# file is (no AWS access in the sandbox that wrote this) -- unit-tested
# here against a hand-built fake cognito-idp client passed in directly
# (see tests/test_admin_api_service.py), which is also simpler to assert
# against than the sys.modules["boto3"] swap the inline-import functions
# need.

_MANAGED_GROUPS = ("Admins", "Editors")


def require_admin_role(caller: dict) -> None:
    """Raises PermissionError if caller["role"] isn't "admin" -- app.py's
    /users routes call this first, before touching Cognito at all (see
    this section's own header comment for why user management
    specifically is the first route ever gated on role in this project).
    PermissionError rather than a bare exception so app.py can map it to
    a 403 without needing to know anything about what check failed."""
    if caller.get("role") != "admin":
        raise PermissionError("Admins only.")


def require_user_pool_id() -> str:
    """Reads COGNITO_USER_POOL_ID (see template.yaml's AdminApiFunction
    Environment block) -- app.py's /users routes call this once per
    request, alongside _cognito_client(), and pass the result into
    whichever of the functions below they need. Raises RuntimeError
    rather than returning None/"" on a missing env var, matching this
    project's usual fail-loud-not-silently-broken posture for
    deployment-config gaps (see e.g. queue_rescrape's own "not
    configured on this deployment" handling above, which instead returns
    a normal, expected {"queued": False, ...} -- the difference here is
    that a misconfigured COGNITO_USER_POOL_ID on the very setup this
    feature depends on is a real deploy bug worth a loud 500, not a
    per-product expected-gap outcome)."""
    user_pool_id = os.environ.get("COGNITO_USER_POOL_ID")
    if not user_pool_id:
        raise RuntimeError("COGNITO_USER_POOL_ID is not configured on this deployment")
    return user_pool_id


def _list_usernames_in_group(cognito_client, user_pool_id: str, group: str) -> list:
    """Paginates list_users_in_group fully (Cognito caps each page, so a
    pool with enough users in one group could otherwise silently miss
    some) and returns just the Username strings -- used both by
    list_users (building the username->group map) and _count_admins
    (the last-Admin lockout guard) below."""
    usernames = []
    kwargs = {"UserPoolId": user_pool_id, "GroupName": group}
    while True:
        resp = cognito_client.list_users_in_group(**kwargs)
        usernames.extend(u["Username"] for u in resp.get("Users", []))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return usernames


def _count_admins(cognito_client, user_pool_id: str) -> int:
    return len(_list_usernames_in_group(cognito_client, user_pool_id, "Admins"))


def list_users(cognito_client, user_pool_id: str) -> list:
    """Lists every user in AdminUserPool, each annotated with whichever of
    Admins/Editors they belong to (None if an account exists but was
    never added to either group -- see admin_api_authorizer's own
    resolve_role_from_groups docstring for why that's a real, valid
    state meaning NO access, not a default role). Cognito's list_users
    doesn't return group membership directly, so this does one
    list_users_in_group call per managed group and builds a
    username->group map from those, rather than one
    admin_list_groups_for_user call per user (O(1) Cognito calls for two
    groups instead of O(n) for n users)."""
    group_by_username = {}
    for group in _MANAGED_GROUPS:
        for username in _list_usernames_in_group(cognito_client, user_pool_id, group):
            group_by_username[username] = group

    users = []
    kwargs = {"UserPoolId": user_pool_id}
    while True:
        resp = cognito_client.list_users(**kwargs)
        for user in resp.get("Users", []):
            username = user["Username"]
            attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
            users.append({
                "username": username,
                "email": attrs.get("email", username),
                "status": user.get("UserStatus"),
                "enabled": user.get("Enabled", True),
                "created_at": user.get("UserCreateDate"),
                "group": group_by_username.get(username),
            })
        token = resp.get("PaginationToken")
        if not token:
            break
        kwargs["PaginationToken"] = token

    users.sort(key=lambda u: u["email"])
    return users


def _generate_initial_password() -> str:
    """Meets AdminUserPool's own PasswordPolicy (12+ chars, upper, lower,
    number required; symbols not required but included here anyway --
    see template.yaml's PasswordPolicy) using stdlib secrets, no extra
    dependency. Never logged or persisted anywhere -- returned exactly
    once, in create_user's own response, for the calling admin to hand
    off out-of-band. This is the SAME two-call pattern (admin_create_user
    + admin_set_user_password with Permanent=True) admin-spa/README.md
    already documents as the manual workaround for phase 1's login form
    not handling Cognito's FORCE_CHANGE_PASSWORD first-login challenge --
    this just automates those two calls from the SPA instead of a human
    typing them, not a new convention."""
    import secrets
    import string

    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*"),
    ]
    pool = string.ascii_uppercase + string.ascii_lowercase + string.digits + "!@#$%^&*"
    required += [secrets.choice(pool) for _ in range(12)]
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


def create_user(cognito_client, user_pool_id: str, email: str, group: str) -> dict:
    """Creates a new Cognito user with a PERMANENT password set
    immediately (not Cognito's own temp-password + FORCE_CHANGE_PASSWORD
    challenge -- see _generate_initial_password's own docstring for why).
    MessageAction="SUPPRESS" skips Cognito's own auto-generated invite
    email since the generated password is returned directly in this
    function's result for the calling admin to relay however they
    choose. Username is the email address itself, matching
    AdminUserPool's UsernameAttributes=["email"] config and this
    project's own documented manual `admin-create-user` command
    (admin-spa/README.md) -- Cognito accepts the email as Username
    directly in this configuration and treats it as the account's own
    sign-in alias, so every later Admin* call in this module also
    addresses this user by email, not some separate opaque id.

    Raises ValueError for an unrecognized group -- the two Cognito groups
    this pool ships with are the only valid destinations, not free text.
    """
    if group not in _MANAGED_GROUPS:
        raise ValueError(f"Unknown group: {group!r}. Must be one of {_MANAGED_GROUPS}.")

    password = _generate_initial_password()
    cognito_client.admin_create_user(
        UserPoolId=user_pool_id,
        Username=email,
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
        MessageAction="SUPPRESS",
        TemporaryPassword=password,
    )
    cognito_client.admin_set_user_password(
        UserPoolId=user_pool_id, Username=email, Password=password, Permanent=True,
    )
    cognito_client.admin_add_user_to_group(
        UserPoolId=user_pool_id, Username=email, GroupName=group,
    )
    return {"email": email, "group": group, "password": password}


def set_user_group(cognito_client, user_pool_id: str, username: str, group: str) -> dict:
    """Moves a user to exactly ONE of the two managed groups, removing
    them from whichever managed group they're currently in first --
    Cognito allows a user to belong to multiple groups, but this
    project's role model (admin_api_authorizer's resolve_role_from_groups)
    treats Admins/Editors as mutually exclusive, so this keeps that
    invariant true rather than letting a user silently accumulate both.

    Raises ValueError for an unrecognized target group, or if this
    change would remove the LAST remaining Admin -- see this section's
    own header comment: user management is the one place a mistake here
    can lock every human out of the SPA (the shared-secret automation
    token still works regardless, but that's scripts-only, not a browser
    login), so this is a real, deliberate guard, not defensive
    boilerplate."""
    if group not in _MANAGED_GROUPS:
        raise ValueError(f"Unknown group: {group!r}. Must be one of {_MANAGED_GROUPS}.")

    current = cognito_client.admin_list_groups_for_user(UserPoolId=user_pool_id, Username=username)
    current_groups = [g["GroupName"] for g in current.get("Groups", [])]

    if "Admins" in current_groups and group != "Admins" and _count_admins(cognito_client, user_pool_id) <= 1:
        raise ValueError(f"Cannot move {username} out of Admins -- they are the last remaining Admin.")

    for name in current_groups:
        if name in _MANAGED_GROUPS and name != group:
            cognito_client.admin_remove_user_from_group(
                UserPoolId=user_pool_id, Username=username, GroupName=name,
            )
    cognito_client.admin_add_user_to_group(
        UserPoolId=user_pool_id, Username=username, GroupName=group,
    )
    return {"username": username, "group": group}


def set_user_enabled(cognito_client, user_pool_id: str, username: str, enabled: bool) -> dict:
    """Disable/re-enable -- the reversible, everyday tool for "this person
    shouldn't have access right now." Prefer this over delete_user for
    routine access changes: a disabled account still shows up in
    list_users for an audit trail and can be flipped back on; a deleted
    one is gone. No last-Admin guard here on purpose -- disabling is
    reversible by another Admin (or by the shared-secret automation
    token via a script), so it doesn't carry the same one-way-lockout
    risk set_user_group/delete_user's guards exist for below."""
    if enabled:
        cognito_client.admin_enable_user(UserPoolId=user_pool_id, Username=username)
    else:
        cognito_client.admin_disable_user(UserPoolId=user_pool_id, Username=username)
    return {"username": username, "enabled": enabled}


def delete_user(cognito_client, user_pool_id: str, username: str) -> dict:
    """Irreversible -- see set_user_enabled above for the reversible
    alternative most day-to-day access changes should use instead.
    Same last-Admin lockout guard as set_user_group: raises ValueError
    rather than deleting the only account that could ever undo the
    mistake."""
    current = cognito_client.admin_list_groups_for_user(UserPoolId=user_pool_id, Username=username)
    current_groups = [g["GroupName"] for g in current.get("Groups", [])]
    if "Admins" in current_groups and _count_admins(cognito_client, user_pool_id) <= 1:
        raise ValueError(f"Cannot delete {username} -- they are the last remaining Admin.")

    cognito_client.admin_delete_user(UserPoolId=user_pool_id, Username=username)
    return {"username": username, "deleted": True}
