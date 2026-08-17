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


# Video-popularity ranking (Al's ask -- see public_api/service.py's
# identical POPULARITY_HALF_LIFE_DAYS/_POPULARITY_SCORE_SQL for the full
# writeup of the decay formula and why 6 months, not the more usual 12,
# given bowling balls' own 6-12 month current-to-retired lifespan). Kept
# in sync BY HAND with that copy -- admin_api and public_api are two
# independently-deployed Lambdas with no shared module between them (same
# per-Lambda-duplicated-constant convention as MAX_VIDEO_IDS_PER_CALL
# elsewhere in this project). Surfaced here too, not just on the public
# API, so Al can see/sort by the actual computed number in the admin
# Products tab and sanity-check it before trusting it on the live site.
POPULARITY_HALF_LIFE_DAYS = 180

# AVERAGE decayed view count per video, times ln(1 + video count) -- NOT
# a plain sum. Al's follow-up, real incident: a raw sum (the original
# shape here) let video COUNT dominate -- a ball with 20 mediocre videos
# could outrank a ball with 4 genuinely popular ones purely on volume.
# ln(1 + count) still gives volume a real, deliberate boost (more
# corroborating videos IS meaningfully more evidence of popularity), just
# sub-linear instead of a straight multiplier: at equal per-video
# quality, 20 videos score ~1.9x a 4-video ball (ln(21)/ln(5)), not the
# old 5x (20/4) a raw sum produced. See public_api/service.py's identical
# copy for the full writeup/worked numbers; kept in sync by hand, same
# no-shared-module reasoning as everything else on this constant.
_POPULARITY_SCORE_SQL = f"""coalesce((
                   select avg(
                       pv.view_count * power(2, -extract(epoch from (now() - coalesce(pv.published_at, pv.created_at))) / (86400.0 * {POPULARITY_HALF_LIFE_DAYS}))
                   ) * ln(1 + count(*))
                   from product_videos pv
                   where pv.product_id = p.id and pv.status = 'approved' and pv.view_count is not null
               ), 0)"""

# Total Average Daily Usage across a product's SKUs (Al: "can we add the
# sum of the ADUs for each product to the main table"). Same trailing-
# window/drops-only ADU definition as admin-site's own computeSkuForecast
# (index.html) -- deliberately re-implemented here in SQL rather than
# shared, same no-shared-module reasoning as POPULARITY_HALF_LIFE_DAYS
# above, but the two MUST stay in lockstep or the Products tab's summary
# number would silently disagree with each product detail page's own
# per-SKU ADU figures. ADU_LOOKBACK_DAYS mirrors admin-site's
# FORECAST_LOOKBACK_DAYS = 30 constant.
#
# Per SKU, within the lookback window: units_sold = sum of only the
# DROPS between consecutive readings (a rise is a restock, excluded --
# same interpretation get_sku_stock_history's own docstring documents);
# elapsed_days = time between the first and last reading in that window;
# sku_adu = units_sold / elapsed_days. A SKU needs at least 2 readings in
# the window to compute a rate at all (matching computeSkuForecast's
# `rows.length < 2 -> adu: null`) -- excluded from the sum via the
# `having count(*) >= 2` below, same effective "contributes nothing, not
# a fabricated zero" behavior as the JS version returning null. Guarded
# division on elapsed_days > 0 for the same edge case computeSkuForecast
# itself guards (`elapsedDays <= 0 -> adu: null`).
#
# Chosen to run unconditionally in the initial GET /products call (same
# tradeoff popularity_score above already made, at the same catalog
# size) rather than a separate async round-trip -- Al offered either
# ("if it need to be an async fetch that is fine... or if it is fast
# enough to just grab in the initial call that is fine too"). If this
# ever turns out to be the slow part of the page at a larger catalog
# size, splitting it into its own endpoint the way needs_video_summary_
# refresh's staleness check stayed OUT of this always-on path is the
# fallback, not a rewrite.
ADU_LOOKBACK_DAYS = 30

_TOTAL_ADU_SQL = f"""coalesce((
                   select sum(case when sku.elapsed_days > 0 then sku.units_sold / sku.elapsed_days else 0 end)
                   from (
                       select
                           h.product_sku_id,
                           sum(case when h.delta < 0 then -h.delta else 0 end) as units_sold,
                           extract(epoch from (max(h.checked_at) - min(h.checked_at))) / 86400.0 as elapsed_days
                       from (
                           select
                               psh.product_sku_id,
                               psh.checked_at,
                               psh.quantity - lag(psh.quantity) over (partition by psh.product_sku_id order by psh.checked_at) as delta
                           from product_sku_stock_history psh
                           join product_skus ps_adu on ps_adu.id = psh.product_sku_id
                           where ps_adu.product_id = p.id
                             and psh.quantity is not null
                             and psh.checked_at >= now() - interval '{ADU_LOOKBACK_DAYS} days'
                       ) h
                       group by h.product_sku_id
                       having count(*) >= 2
                   ) sku
               ), 0)"""

# Common-sense sort options for the Products tab's "Sort" control -- Al's
# ask: "lets add some common sense sort options for both the admin and
# consumer UIs". Identical to public_api/service.py's copy (kept in sync
# by hand, same no-shared-module reasoning as POPULARITY_HALF_LIFE_DAYS
# above) -- see that copy's comment for why 'newest'/'oldest' use
# release_date (not created_at/updated_at) with an explicit `nulls last`
# in both directions.
_SORT_ORDER_BY = {
    "popularity": "popularity_score desc, p.id asc",
    "newest": "p.release_date desc nulls last, p.id asc",
    "oldest": "p.release_date asc nulls last, p.id asc",
    "name_asc": "p.name asc, p.id asc",
    "name_desc": "p.name desc, p.id asc",
}
_DEFAULT_ORDER_BY = "p.updated_at desc, p.id asc"


def list_products(conn, published: bool = None, brand_id: str = None, search: str = None,
                   needs_video_summary_refresh: bool = None, has_approved_video_summaries: bool = None,
                   missing_core: bool = None, missing_coverstock: bool = None, missing_skus: bool = None,
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

    popularity_score (see _POPULARITY_SCORE_SQL above) is always
    computed and returned, same as public_api's copy of this query --
    cheap enough at this catalog's size to include unconditionally, so
    the Products tab can show the column without a separate round-trip
    even when not sorting by it. Accepted sort values (see
    _SORT_ORDER_BY above): 'popularity' (desc), 'newest'/'oldest'
    (release_date), 'name_asc'/'name_desc' (alphabetical). Anything else
    (including the default None) keeps the existing updated_at-desc
    order, same unrecognized-value-is-harmless convention as every other
    filter/sort value on this endpoint.

    total_adu (see _TOTAL_ADU_SQL above) is likewise always computed and
    returned -- Al: "can we add the sum of the ADUs for each product to
    the main table." No sort option added for it (not asked for)."""
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
    query = f"""
        select p.id, p.brand_id, b.name as brand_name, p.name, p.url, p.status, p.published, p.updated_at,
               p.core_id, c.name as core_name, p.release_date, p.coverstock_id, p.coverstock_name,
               {_POPULARITY_SCORE_SQL} as popularity_score,
               {_TOTAL_ADU_SQL} as total_adu
        from products p
        left join cores c on c.id = p.core_id
        left join brands b on b.id = p.brand_id
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
