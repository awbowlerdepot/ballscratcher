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


def list_products(conn, published: bool = None, brand_id: str = None, search: str = None,
                   needs_video_summary_refresh: bool = None, has_approved_video_summaries: bool = None,
                   missing_core: bool = None, limit: int = 50, offset: int = 0) -> list:
    """needs_video_summary_refresh=True: products with at least one
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
    doesn't expose a parseable core name."""
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
    query = """
        select p.id, p.brand_id, b.name as brand_name, p.name, p.url, p.status, p.published, p.updated_at,
               p.core_id, c.name as core_name
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
    # id as a final tiebreaker -- same reason list_video_candidates and
    # fetch_products_to_search needed one (see admin_api/service.py's own
    # earlier fix and video_discovery/app.py's ROTATION section): rows
    # sharing an updated_at value make plain OFFSET/LIMIT pagination
    # unstable, and this endpoint is now paginated by a real consumer
    # (the backfill script) as of this filter's addition.
    query += " order by p.updated_at desc, p.id asc limit %s offset %s"
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

        cur.execute(
            "select * from product_images where product_id = %s",
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
    the ordering -- and therefore the pagination -- is fully deterministic."""
    query = """
        select pv.id, pv.product_id, p.name as product_name, b.name as brand_name,
               pv.youtube_video_id, pv.title, pv.channel_title, pv.published_at,
               pv.thumbnail_url, pv.match_query, pv.match_confidence,
               pv.transcript_note, pv.status, pv.source,
               pv.created_at, pv.resolved_at, pv.resolved_by,
               (pv.summary is not null) as has_summary
        from product_videos pv
        join products p on p.id = pv.product_id
        join brands b on b.id = p.brand_id
        where pv.status = %s
    """
    params = [status]
    if product_id:
        query += " and pv.product_id = %s"
        params.append(product_id)
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


def reassign_video_candidate(conn, video_id: str, new_product_id: str) -> dict:
    """Moves a video candidate to a different product. Built for a real,
    known failure mode of video_discovery's score_match heuristic (see its
    module docstring): 'high' confidence only requires the brand name plus
    ANY ONE significant product-name token in the title, so e.g. "Storm
    Absolute Power Review" scores 'high' for the "Storm Absolute" product
    too, not just "Storm Absolute Power" -- a real, accepted tradeoff of
    auto-approving 'high' matches in bulk (see
    scripts/auto_approve_video_candidates.py's docstring), not something
    this function tries to prevent. This is the correction tool for when
    it happens: works regardless of status (pending/approved/rejected),
    and deliberately does NOT touch transcript/transcript_note/summary --
    if a transcript was already fetched under the wrong product, it's
    still a real transcript of that same YouTube video, no reason to lose
    it and refetch under the correct product.

    Checked, not caught: looks for an existing (new_product_id,
    youtube_video_id) row before updating, rather than attempting the
    update and catching a unique-constraint violation from the DB driver --
    a clearer, more actionable error this way ("one already exists over
    there, delete a duplicate first") than a raw IntegrityError, and this
    is an admin tool used by one person at a time, so the small
    check-then-act race window is an acceptable tradeoff. If that
    conflict fires, delete_video_candidate below is the cleanup path."""
    with conn.cursor() as cur:
        cur.execute("select id, youtube_video_id from product_videos where id = %s", (video_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_videos row with id {video_id}")
        youtube_video_id = row[1]

        cur.execute("select id from products where id = %s", (new_product_id,))
        if cur.fetchone() is None:
            raise LookupError(f"No products row with id {new_product_id}")

        cur.execute(
            "select id from product_videos where product_id = %s and youtube_video_id = %s",
            (new_product_id, youtube_video_id),
        )
        conflict = cur.fetchone()
        if conflict is not None:
            raise ValueError(
                f"product {new_product_id} already has a product_videos row for "
                f"youtube_video_id={youtube_video_id} (id={conflict[0]}) -- delete "
                f"one of the two duplicates first (see delete_video_candidate), "
                f"then retry the reassignment"
            )

        cur.execute("update product_videos set product_id = %s where id = %s", (new_product_id, video_id))
    conn.commit()
    return {"video_id": video_id, "product_id": new_product_id}


def delete_video_candidate(conn, video_id: str) -> dict:
    """Hard delete -- distinct from reject_video_candidate, which only
    marks status='rejected' and keeps the row for audit. Built as the
    cleanup step for reassign_video_candidate's conflict case above: two
    product_videos rows for the same (product_id, youtube_video_id) pair
    can't coexist (see 004_product_videos.sql's unique constraint), but two
    DIFFERENT products can each have their own row for the same YouTube
    video (a real, legitimate case -- one review video can genuinely cover
    two products), or a video can get moved to the correct product while a
    stale duplicate is left sitting under the wrong one. Deleting the
    wrong copy is a normal, expected cleanup action here, not a mistake to
    guard against with extra confirmation steps -- same one-admin-at-a-time
    reasoning as reassign_video_candidate above."""
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
