"""
Business logic for the public, unauthenticated read-only API -- the data
source for the consumer-facing site (Al: "let's start on the consumer
facing site... single page like site... a page to view the bowling ball
details... an intuitive way to populate a ball comparison page... focus
on current bowling balls and a way to still view retired balls and
suggest current balls that best compare to the retired balls").

Deliberately a SEPARATE Lambda/module from admin_api, not new routes
bolted onto it, even though both read the same Postgres schema. Three
real reasons, not just tidiness:
  1. admin_api sits behind AdminHttpApi's shared-secret Lambda authorizer
     (see AdminApiAuthorizerFunction) -- every route there is meant to
     require a bearer token. This API is the opposite: meant to be
     wide open, no auth at all, callable directly from a browser running
     on someone else's computer. Mixing "requires a token" and "must
     never require a token" behind one function/one HttpApi resource is
     a foot-gun waiting to leak an admin-only field or endpoint publicly,
     or (worse) accidentally gate a real public endpoint behind auth and
     break the site.
  2. products.published exists SPECIFICALLY for this (see
     001_init_schema.sql's own comment: "gates what the consumer site /
     BowlerDepot sync can see") -- this module is the first real
     consumer of that gate. Every query in here filters published = true
     unconditionally; there is no parameter that can turn it off, unlike
     admin_api's list_products(published=...) which defaults to showing
     everything so an admin can review the unpublished backlog.
  3. Response shapes here are curated for a storefront (cards, detail
     pages, comparison grids) rather than admin_api's "expose every
     column so data-quality gaps are visible by inspection" philosophy
     (see admin_api.get_product's own docstring) -- deliberately NOT
     select *, deliberately omitting internal bookkeeping (scrape_status,
     source_platform, discovered_url, bowlerdepot_matches/bowwwl_matches,
     transcript raw text, match_confidence, resolved_by, ...) that a
     public visitor has no use for and that in a couple of cases (raw
     scrape/reconciliation internals) shouldn't be exposed at all.

Same plain-functions-plus-psycopg2-connection shape as admin_api/
service.py, split from app.py's FastAPI routing layer for the same
reason: fastapi/pydantic aren't installable in this sandbox (pip's proxy
returns 403), so this file is what's actually unit tested (see
tests/test_public_api_service.py) and app.py's routes are logic-verified
only, not executed.
"""
import json
import os

# Module-level cache, deliberately NOT function-local -- see get_db_
# connection's own docstring for why this is the whole point.
_cached_conn = None
_cached_secret = None


def _connect_with_secret(secret):
    import psycopg2

    conn = psycopg2.connect(
        host=secret["host"],
        port=secret.get("port", 5432),
        dbname=secret["dbname"],
        user=secret["username"],
        password=secret["password"],
    )
    # This API is entirely read-only (see this module's own docstring) --
    # autocommit means a bare SELECT never leaves an implicit transaction
    # open, so there's nothing to commit/rollback and nothing sitting
    # "idle in transaction" on this connection between requests for
    # however long the Lambda container stays warm.
    conn.autocommit = True
    return conn


def _fetch_secret():
    import boto3

    secret_arn = os.environ["DB_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    return json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])


def get_db_connection():
    """Returns a connection REUSED across warm Lambda invocations instead
    of opening a fresh one (plus a Secrets Manager round trip) on every
    single request. REAL PERFORMANCE INCIDENT: Al, "the public site is
    pretty slow" -- root-caused to the old version of this function
    paying a fresh TCP/TLS/Postgres-auth handshake AND a
    secretsmanager:GetSecretValue call on every request, even on an
    already-warm container, since nothing was ever cached across
    invocations.

    Caches both the resolved secret (host/port/dbname/user/password don't
    change between requests -- Secrets Manager is only re-queried if a
    connection attempt using the cached secret actually fails, covering a
    real credential rotation) and the open connection itself at MODULE
    level, which Lambda's execution-context reuse keeps alive across
    invocations on the same warm container -- the standard idiom for
    connection reuse in Lambda (no RDS Proxy in front of this database,
    so this is the cheap alternative).

    A cheap `select 1` health-checks the cached connection before
    returning it -- `conn.closed == 0` alone only reflects whether THIS
    process ever closed it, not whether the server or an intermediate
    network hop dropped it (RDS failover, idle timeout, etc), so a real
    round trip is the only reliable check. On any failure, discards the
    cached connection and reconnects using the cached secret; if THAT
    also fails, treats the secret itself as stale (a real rotation) and
    re-fetches it from Secrets Manager exactly once before giving up,
    rather than getting stuck on a dead cached secret for the rest of
    this container's warm lifetime.

    Callers should NOT call conn.close() when done -- app.py's routes no
    longer do (see that module) -- closing would defeat the whole point
    by forcing the next request on this same warm container to pay the
    handshake cost again."""
    import psycopg2

    global _cached_conn, _cached_secret

    if _cached_conn is not None:
        try:
            with _cached_conn.cursor() as cur:
                cur.execute("select 1")
            return _cached_conn
        except Exception:
            try:
                _cached_conn.close()
            except Exception:
                pass
            _cached_conn = None

    if _cached_secret is None:
        _cached_secret = _fetch_secret()

    try:
        _cached_conn = _connect_with_secret(_cached_secret)
    except psycopg2.OperationalError:
        # Cached secret might be stale (a real credential rotation) --
        # force one fresh Secrets Manager read and retry once.
        _cached_secret = _fetch_secret()
        _cached_conn = _connect_with_secret(_cached_secret)

    return _cached_conn


# --------------------------------------------------------------------
# Brands (filter facet for Browse)
# --------------------------------------------------------------------

def list_brands(conn) -> list:
    """Every brand with at least one published product -- unlike
    admin_api.list_brands (every brand, full stop, for an admin's filter
    dropdown where an empty/unpublished-only brand is still worth
    seeing), a public browse page has no reason to offer a brand filter
    option that would always return zero results."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct b.id, b.name
            from brands b
            join products p on p.brand_id = b.id
            where p.published = true
            order by b.name
            """
        )
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


# --------------------------------------------------------------------
# Browse / list
# --------------------------------------------------------------------

# Video-popularity ranking: Al's ask -- "can we build in a view_count time
# decay so that older videos will organically move down a 'popular'
# ranking when summed up for a ball ... We will not have a way to see what
# videos do over time so I feel like applying some version of a time decay
# is the next best thing." popularity_score used to be computed HERE, as a
# correlated subquery run on every single list_products call (same
# exponential-decay-by-video-age formula admin_api/service.py's identical
# copy used) -- REAL PERFORMANCE INCIDENT (2026-09-06, see
# 030_materialized_product_scores.sql's own header comment for the full
# writeup): this was the literal public-facing Browse page cost Al asked
# about, recomputed per card on every page load. The formula (half-life
# decay, HALF_LIFE_DAYS=180 chosen for a bowling ball's real 6-12 month
# retirement lifespan, AVERAGE decayed view count * ln(1 + video count) so
# volume can't dominate quality) now lives in exactly one place:
# src/refresh_product_scores/app.py, which recomputes it once a day into
# the real products.popularity_score column instead. list_products below
# just reads that column now -- see products.popularity_score's own
# column comment (030) for the formula history if you're looking for it.

# Common-sense sort options for the Browse page's "Sort" control -- Al's
# ask: "lets add some common sense sort options for both the admin and
# consumer UIs". Keyed by the exact ?sort= value; every branch keeps the
# same `p.id asc` tiebreaker the pre-existing default/popularity branches
# already used (see list_products' own tiebreaker comment) -- pagination
# has to stay stable no matter which column is doing the primary
# ordering. 'newest'/'oldest' sort by `release_date`, not `created_at`/
# `updated_at` -- a shopper cares when a ball actually came out, not when
# this project happened to scrape it. `release_date` is nullable (not
# every scrape captures it), so both directions say `nulls last`
# explicitly -- Postgres's own default for a plain `desc` sort is `nulls
# first`, which would otherwise push every ball with an unknown release
# date to the very top of "Newest". Kept in sync by hand with admin_api/
# service.py's identical copy, same no-shared-module reasoning as this
# project's other hand-synced per-Lambda constants.
# REAL BUG, fixed alongside admin_api/service.py's identical copy of this
# dict (kept in sync by hand): Al reported the admin Products tab's
# "oldest" sort didn't put the newest products at the end. Root cause --
# release_date is manufacturer-published and sparse (most of the catalog
# never had one to parse), so the original `nulls last` ordering dumped
# the majority of the catalog into one block after every dated row, tie-
# broken only by p.id -- a random uuid_generate_v4() primary key, not
# anything chronological. Fixed by sorting on `coalesce(p.release_date,
# p.first_seen_at::date)` instead: first_seen_at (003_date_tracking_and_
# bowwwl.sql) is `not null default now()`, set the moment a scraper first
# INSERTs a product row, so every row now gets one real position in a
# single unified chronological ordering (real release_date when known,
# discovery date as an honest fallback when not) instead of a real-dates
# block followed by an effectively-unordered everything-else block. See
# admin_api/service.py's own copy of this comment for the fuller
# writeup.
_SORT_ORDER_BY = {
    "popularity": "p.popularity_score desc, p.id asc",
    "newest": "coalesce(p.release_date, p.first_seen_at::date) desc, p.id asc",
    "oldest": "coalesce(p.release_date, p.first_seen_at::date) asc, p.id asc",
    "name_asc": "p.name asc, p.id asc",
    "name_desc": "p.name desc, p.id asc",
}
_DEFAULT_ORDER_BY = "p.updated_at desc, p.id asc"


def list_products(conn, status: str = "current", brand_id: str = None, core_id: str = None,
                   coverstock_id: str = None, search: str = None, sort: str = None,
                   limit: int = 24, offset: int = 0) -> list:
    """Card-shaped results for the Browse page -- one row per published
    product, curated to what a browse card actually needs (not admin_
    api.list_products' broader admin-focused column set, and never
    admin_api's own missing_core/missing_coverstock/source_platform data-
    quality filters, which have no meaning to a site visitor).

    status defaults to 'current' (not None/'any' the way admin_api's
    equivalent filter does) -- Al's direct ask: "a focus on current
    bowling balls and a way to still view retired balls". A caller has to
    explicitly pass status='retired' to see the retired catalog; there's
    no bare "everything" mode here on purpose, current is the front door.

    published = true is hardcoded into the query itself, not a parameter
    -- see this module's own docstring for why that's non-negotiable here
    (contrast admin_api.list_products' published: bool = None, which
    defaults to showing both so an admin can review the unpublished
    backlog).

    Card fields: id/name/url/color/status, brand_name, core_name/
    core_type, coverstock_name/coverstock_type/coverstock_material,
    release_date, primary_image_url (prefers the visible product_images
    row an admin has flagged is_thumbnail -- see migration 010's own
    comment for why that flag, not the raw products.primary_image_url
    column, is the actual source of truth for "which image is the hero
    image": an admin can retarget the thumbnail via PATCH /products/
    {id}/images/{image_id} at any time without that column ever being
    touched, so reading it directly here could show a stale image Al
    had already re-flagged away from in the admin UI. Falls back to
    the first visible image by display_order if nothing is flagged,
    then to the raw column as a last resort for a legacy row with no
    product_images rows at all), and video_reviews_summary_video_count
    (so a card can show "based on N video reviews" without a second
    round-trip -- video_reviews_summary's actual TEXT is left for the
    detail page, a card has no room for it).

    popularity_score is always selected (a plain products.popularity_score
    column read as of migration 030 -- see this module's own header
    comment above for the real performance incident that prompted storing
    it instead of computing it live here) -- cheap enough to include on
    every call, not gated behind sort='popularity', so a card can show a
    "trending" indicator even when the visitor is browsing in the default
    order.

    sort: None (default) keeps the existing 'updated_at desc' order --
    most-recently-touched-by-a-scraper first, which is really "recently
    changed", not "popular". Accepted values (see _SORT_ORDER_BY above):
    'popularity' (the view-count-decay ranking -- Al's ask, formula lives
    in refresh_product_scores/app.py now), 'newest'/'oldest' (release_date),
    'name_asc'/'name_desc' (alphabetical). Any other value (including
    None) is silently ignored and falls back to the default order, same
    unrecognized-value-is-harmless convention every other filter on this
    endpoint already follows."""
    query = f"""
        select p.id, p.name, p.url, p.color, p.status,
               b.name as brand_name,
               c.name as core_name, c.core_type,
               p.coverstock_name, p.coverstock_type, p.coverstock_material,
               p.release_date,
               coalesce(
                   (
                       select pi.stored_url from product_images pi
                       where pi.product_id = p.id and pi.is_visible = true
                       order by pi.is_thumbnail desc, pi.display_order, pi.id
                       limit 1
                   ),
                   p.primary_image_url
               ) as primary_image_url,
               p.video_reviews_summary_video_count,
               p.popularity_score
        from products p
        join brands b on b.id = p.brand_id
        left join cores c on c.id = p.core_id
        where p.published = true and p.status = %s
    """
    params = [status]
    if brand_id:
        query += " and p.brand_id = %s"
        params.append(brand_id)
    if core_id:
        query += " and p.core_id = %s"
        params.append(core_id)
    if coverstock_id:
        query += " and p.coverstock_id = %s"
        params.append(coverstock_id)
    if search:
        query += " and p.name ilike %s"
        params.append(f"%{search}%")
    # id as a tiebreaker -- same reason every other paginated list in this
    # project needs one (see admin_api.list_products' own comment): rows
    # sharing an updated_at/release_date value (or, now, a popularity_score
    # value -- e.g. two products both with zero approved-video views) make
    # plain OFFSET/LIMIT pagination unstable once there's a real paginated
    # consumer (the Browse page) rather than a one-shot admin listing.
    query += " order by " + _SORT_ORDER_BY.get(sort, _DEFAULT_ORDER_BY) + " limit %s offset %s"
    params += [limit, offset]

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


# --------------------------------------------------------------------
# Articles (Learn section index -- see bowlerdepot-learn/ for the site
# that consumes this. Al: "i would like this to be a sophisticated learn
# section with articles that are displayed in a way that is best from a
# UI/UX perspective and bigcommerce is just not the place" -- this is the
# new list/browse endpoint that request needed; GET /products/{id}/
# article above already covered a single article, but there was no way
# to browse the catalog of articles at all until now.)
# --------------------------------------------------------------------

# Sort options for the Learn index -- deliberately a SMALLER set than
# list_products'/_SORT_ORDER_BY above (no 'popularity': articles have no
# view-count/video data of their own to rank by, and reusing a product's
# popularity_score here would rank articles by how popular the BALL is,
# not the article, which isn't the same thing and would need its own
# separate join for no clear benefit yet). 'newest'/'oldest' sort by
# reviewed_at (when an admin actually approved/published the article),
# NOT generated_at (when product_article_generator first drafted it) --
# a visitor's "newest articles" should mean "most recently made public",
# same distinction as list_products' own newest/oldest using release_date
# rather than created_at/updated_at. nulls last on both directions for
# the same reason list_products documents: every row selected here
# already has status='approved' (see list_articles' own where clause),
# so reviewed_at should always be set in practice, but nulls last is a
# harmless defensive default rather than assuming that invariant holds
# forever. Kept as its own dict (not merged into _SORT_ORDER_BY above)
# since the column being sorted on lives on product_articles, not
# products -- the alias prefix genuinely differs.
_ARTICLE_SORT_ORDER_BY = {
    "newest": "pa.reviewed_at desc nulls last, pa.id asc",
    "oldest": "pa.reviewed_at asc nulls last, pa.id asc",
    "title_asc": "pa.title asc, pa.id asc",
    "title_desc": "pa.title desc, pa.id asc",
}
_ARTICLE_DEFAULT_ORDER_BY = "pa.reviewed_at desc nulls last, pa.id asc"


def list_categories(conn) -> list:
    """Learn-site content taxonomy (migration 031) -- Al, picking a Learn
    theme: "having Categories with one being Bowling balls and Ball
    review being a type of article. Just to ensure future expansion."
    Every category with its article_types nested inline (mirrors admin_
    api's own list_categories -- see that module's docstring for the
    fuller rationale on why this is its own small lookup taxonomy,
    deliberately decoupled from products.product_type). Backs the Learn
    site's nav/eyebrow labels so "Bowling Balls" / "Ball Review" are read
    off this data rather than hardcoded into a component -- a future
    category or article_type just needs a row here, not a frontend
    deploy."""
    with conn.cursor() as cur:
        cur.execute(
            "select id, slug, name, description, display_order from categories order by display_order, name"
        )
        columns = [desc[0] for desc in cur.description]
        categories = [dict(zip(columns, row)) for row in cur.fetchall()]

        cur.execute(
            """
            select id, category_id, slug, name, description, display_order
            from article_types order by display_order, name
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


def list_articles(conn, brand_id: str = None, coverstock_id: str = None, category_id: str = None,
                   search: str = None, sort: str = None, limit: int = 24, offset: int = 0) -> list:
    """Card-shaped results for the Learn section's browse/index page --
    one row per APPROVED article whose product is still published, same
    published-is-non-negotiable posture as every other route in this
    module (see this module's own header docstring) plus the extra
    status = 'approved' gate get_product_article already applies to a
    single article (a 'pending'/'rejected' row has no business being
    listed any more than it has being served individually).

    brand_id/coverstock_id filter through to the underlying PRODUCT the
    article is about (an article has no brand/coverstock of its own --
    it's a review of a specific ball), same filter names as
    list_products' own brand_id/coverstock_id so the Learn site's filter
    UI can reuse the exact same /brands-sourced dropdown the Browse page
    already has. search matches article title OR hook (not the product
    name -- list_products' search already covers "find this ball by
    name"; this is "find this article by what it's actually about/says"),
    same `ilike` substring-match convention as list_products' search.

    Card fields are deliberately narrow, same reasoning as list_products'
    own docstring: title/hook (not performance_summary/verdict/pros/cons/
    etc -- those are detail-page content, a card just needs a headline
    and teaser), generated_at/reviewed_at, plus enough of the underlying
    product (id, name, url, brand_name, coverstock_name,
    primary_image_url -- same thumbnail-flag-then-fallback coalesce as
    every other card query in this module) for a Learn card to link to
    and preview the ball itself without a second round-trip.

    product_shot_image_url is the article's own AI-generated stylized
    product hero shot (023_product_article_images.sql), NOT the ball's
    real scraped photo -- Al: "can we use the product shot for the card
    in the list of review articles" -- so the card's <img> can prefer a
    consistent, premium-looking editorial shot over the raw catalog
    photo. Nullable: image generation is a second, independently-fallible
    step (see that migration's own comment), so some articles will only
    ever have primary_image_url. The frontend is responsible for the
    product_shot_image_url-then-primary_image_url fallback (same pattern
    ArticleDetailPage.tsx/prerender.ts already use for the detail page's
    hero image) -- this function returns both real fields rather than
    picking one in SQL, so a caller that genuinely wants the raw photo
    (e.g. a future admin view) still can.

    sort: None (default, see _ARTICLE_DEFAULT_ORDER_BY) is "most
    recently approved/published first" -- the sensible default landing
    order for a Learn index, mirroring list_products' own
    most-recently-touched default. See _ARTICLE_SORT_ORDER_BY above for
    the full accepted set; any other value (including None) falls back
    to the default, same unrecognized-value-is-harmless convention
    list_products already follows.

    category_name/category_slug/article_type_name/article_type_slug
    (migration 031) -- left-joined, so an older article row generated
    before this migration's backfill ran (or a not-yet-onboarded
    product_type -- see resolve_category_and_article_type's docstring in
    product_article_generator/app.py) still lists, just with those four
    fields null. category_id filters the same way brand_id/coverstock_id
    do, scoped to pa.category_id (the article's own persisted category,
    not a live join back through product_type)."""
    query = f"""
        select pa.id as article_id, pa.title, pa.hook, pa.generated_at, pa.reviewed_at,
               pa.product_shot_image_url,
               p.id as product_id, p.name as product_name, p.url as product_url,
               b.name as brand_name,
               p.coverstock_name, p.coverstock_type,
               cat.name as category_name, cat.slug as category_slug,
               atype.name as article_type_name, atype.slug as article_type_slug,
               coalesce(
                   (
                       select pi.stored_url from product_images pi
                       where pi.product_id = p.id and pi.is_visible = true
                       order by pi.is_thumbnail desc, pi.display_order, pi.id
                       limit 1
                   ),
                   p.primary_image_url
               ) as primary_image_url
        from product_articles pa
        join products p on p.id = pa.product_id
        join brands b on b.id = p.brand_id
        left join categories cat on cat.id = pa.category_id
        left join article_types atype on atype.id = pa.article_type_id
        where pa.status = 'approved' and p.published = true
    """
    params = []
    if brand_id:
        query += " and p.brand_id = %s"
        params.append(brand_id)
    if coverstock_id:
        query += " and p.coverstock_id = %s"
        params.append(coverstock_id)
    if category_id:
        query += " and pa.category_id = %s"
        params.append(category_id)
    if search:
        query += " and (pa.title ilike %s or pa.hook ilike %s)"
        params.append(f"%{search}%")
        params.append(f"%{search}%")
    # id as a tiebreaker -- same reason list_products needs one (see its
    # own comment): rows sharing a reviewed_at value (e.g. a batch of
    # articles all approved in the same admin session) make plain
    # OFFSET/LIMIT pagination unstable otherwise.
    query += " order by " + _ARTICLE_SORT_ORDER_BY.get(sort, _ARTICLE_DEFAULT_ORDER_BY) + " limit %s offset %s"
    params += [limit, offset]

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


# --------------------------------------------------------------------
# Detail
# --------------------------------------------------------------------

def get_product(conn, product_id: str):
    """Full detail-page payload for one published product. Returns None
    -- not just for a nonexistent id, but ALSO for a real, existing,
    unpublished one -- app.py maps both to an identical 404. This is
    deliberate, not an oversight: a public detail page must not
    distinguish "doesn't exist" from "exists but isn't published yet"
    the way admin_api.get_product can (an admin is allowed to know the
    difference; a site visitor has no legitimate reason to probe for
    unpublished product ids)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select p.id, p.name, p.url, p.color,
                   p.coverstock_material, p.coverstock_type, p.coverstock_name,
                   p.has_particle, p.has_custom_graphic, p.factory_finish,
                   p.part_number,
                   lower(p.weights_available) as weights_min,
                   upper(p.weights_available) as weights_max,
                   p.usbc_approval_date,
                   p.release_date, p.description, p.status,
                   coalesce(
                       (
                           select pi.stored_url from product_images pi
                           where pi.product_id = p.id and pi.is_visible = true
                           order by pi.is_thumbnail desc, pi.display_order, pi.id
                           limit 1
                       ),
                       p.primary_image_url
                   ) as primary_image_url,
                   p.video_reviews_summary, p.video_reviews_summary_video_count,
                   p.video_reviews_summary_updated_at,
                   b.id as brand_id, b.name as brand_name,
                   m.name as manufacturer_name,
                   c.id as core_id, c.name as core_name, c.core_type,
                   cs.id as coverstock_id, cs.name as coverstock_full_name
            from products p
            join brands b on b.id = p.brand_id
            left join manufacturers m on m.id = b.manufacturer_id
            left join cores c on c.id = p.core_id
            left join coverstocks cs on cs.id = p.coverstock_id
            where p.id = %s and p.published = true
            """,
            (product_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cur.description]
        product = dict(zip(columns, row))

        # weights_available is stored as an int4range (see
        # 001_init_schema.sql), not a plain string -- selecting p.
        # weights_available directly and handing the raw range value to
        # FastAPI's jsonable_encoder produced "{}" on the wire (a real bug
        # caught by Al: the consumer site's product detail page crashed
        # with "Objects are not valid as a React child" trying to render
        # that empty object). Pulling lower()/upper() as plain ints here
        # and formatting a human string sidesteps the range-type
        # serialization problem entirely. Postgres normalizes a discrete
        # range to canonical form on write ("[12,16]" in becomes
        # "[12,17)" stored), so upper() is exclusive -- subtract 1 to get
        # the real max weight back.
        weights_min = product.pop("weights_min", None)
        weights_max = product.pop("weights_max", None)
        if weights_min is not None and weights_max is not None:
            product["weights_available"] = f"{weights_min}-{weights_max - 1} lb"
        else:
            product["weights_available"] = None

        cur.execute(
            """
            select weight_lbs, rg, differential, mass_bias
            from product_skus
            where product_id = %s
            order by weight_lbs desc
            """,
            (product_id,),
        )
        sku_columns = [desc[0] for desc in cur.description]
        product["skus"] = [dict(zip(sku_columns, row)) for row in cur.fetchall()]

        # Only visible images (migration 010's is_visible flag -- an admin
        # can hide a stray/bad image without deleting the row), ordered by
        # the admin-curated display_order, is_thumbnail surfaced so the
        # frontend knows which one to use as the hero image.
        cur.execute(
            """
            select id, image_type, stored_url, is_thumbnail, display_order
            from product_images
            where product_id = %s and is_visible = true
            order by display_order, id
            """,
            (product_id,),
        )
        image_columns = [desc[0] for desc in cur.description]
        product["images"] = [dict(zip(image_columns, row)) for row in cur.fetchall()]

        # Only approved AND summarized videos -- a 'pending'/'rejected'
        # row, or an approved one video_summarizer hasn't finished
        # processing yet (summary still null), has no business on a
        # public page. transcript itself is deliberately NOT selected
        # here (it's the raw, sometimes-messy caption text video_
        # summarizer's own prompt consumes, not something meant for
        # display -- summary is the polished, public-facing version of
        # the same content). youtube_video_id is what the frontend needs
        # to actually embed the player.
        cur.execute(
            """
            select youtube_video_id, title, channel_title, published_at,
                   thumbnail_url, summary
            from product_videos
            where product_id = %s and status = 'approved' and summary is not null
            order by published_at desc nulls last
            """,
            (product_id,),
        )
        video_columns = [desc[0] for desc in cur.description]
        product["videos"] = [dict(zip(video_columns, row)) for row in cur.fetchall()]

        return product


def get_video_summary_by_bigcommerce_product_id(conn, bigcommerce_product_id: str) -> dict:
    """Backs the small embed script Al's asked to run on live BowlerDepot
    (BigCommerce) product pages: reads the BigCommerce product id straight
    off the storefront page's own add-to-cart form (confirmed live --
    every Stencil PDP has <input name="product_id">), calls this route,
    and inserts the returned video_reviews_summary paragraph into the
    theme's existing native Videos tab. See src/bowlerdepot_video_sync/
    app.py's module docstring for the sibling piece (pushing individual
    videos into BigCommerce's own Product Videos feature) -- that part
    needs no public route at all (server-to-server), only this aggregate
    rollup-summary lookup does, since there's no native BigCommerce slot
    for an aggregate paragraph the way there is for a per-video
    description.

    Deliberately always returns 200 with video_reviews_summary: None
    rather than 404 when there's no match/no summary yet -- this is the
    NORMAL case for most of BowlerDepot's catalog (only products
    bowlerdepot_reconciliation has confidently matched, and only once
    video_summarizer has actually produced a rollup, ever have one), not
    an error condition the embed script needs special-case handling for;
    it just no-ops when the field is null.

    Joined through bowlerdepot_products the same way admin_api surfaces
    bowlerdepot_matches, but scoped to match_status = 'matched' only (an
    'ambiguous'/'unmatched' row is a known-unreliable match by that
    module's own design -- showing a summary for the WRONG product on
    BowlerDepot would be worse than showing nothing) and p.published =
    true (same public-only gate every other route in this module
    enforces unconditionally)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select p.video_reviews_summary, p.video_reviews_summary_video_count
            from bowlerdepot_products bp
            join products p on p.id = bp.product_id
            where bp.bigcommerce_product_id = %s
              and bp.match_status = 'matched'
              and p.published = true
            """,
            (bigcommerce_product_id,),
        )
        row = cur.fetchone()
        if row is None:
            return {"video_reviews_summary": None, "video_reviews_summary_video_count": 0}
        summary, video_count = row
        return {"video_reviews_summary": summary, "video_reviews_summary_video_count": video_count}


def get_product_article(conn, product_id: str):
    """Backs GET /products/{id}/article -- the read side of 022_product_
    articles.sql. Al: "this could be the backend that pulls together all
    the creative and content for the frontend" for a bowling.com-style
    ball review article on bowlerdepot.com.

    Returns None only when product_id doesn't resolve to a real,
    published product -- app.py maps that to 404, same as get_product's
    own "doesn't exist" vs "exists but unpublished" non-distinction (see
    get_product's docstring for why: a public route must not let a
    visitor distinguish the two).

    Otherwise ALWAYS returns 200 with {"product_id": ..., "article": ...},
    article being None when there's no APPROVED article yet -- same
    always-200-null-field contract as get_video_summary_by_bigcommerce_
    product_id above. This is the normal case for most of the catalog (an
    article only exists once product_article_generator has run AND an
    admin has approved it -- see 022_product_articles.sql's header
    comment), not an error condition a frontend needs special-case
    handling for. A 'pending'/'rejected' article is invisible here even
    if one exists -- same review-gate reasoning admin_api's whole
    approve/reject workflow exists for in the first place.

    Spec values (core/coverstock/RG/differential/mass_bias) are
    deliberately NOT read off product_articles -- they're joined live
    against products/product_skus/cores here, exactly as migration 022's
    header comment describes, so a spec correction elsewhere never
    requires regenerating the article to stay accurate. Same reasoning
    for comparison_table: sibling_product_ids on the article row is only
    ever a heuristic id list (see that migration's own caveat on it not
    being ground truth); the actual comparison_table rows returned here
    are built fresh from each sibling's CURRENT live data, and any
    sibling that's since been unpublished or deleted silently drops out
    of the table rather than erroring the whole article -- a stale
    heuristic reference shouldn't be able to break an otherwise-good,
    already-approved article.

    action_shot_image_url/product_shot_image_url (023_product_article_
    images.sql) ARE read straight off the article row, unlike the spec
    values above -- these are AI-generated content proper to the article
    itself (not live product data with a separate source of truth to stay
    in sync with), so there's nothing to join live. Either or both may be
    null if image generation hasn't run yet or didn't succeed for this
    article -- a frontend should treat a null image URL as "no image",
    not an error.

    ecommerce_url (Al: "would it be possible to link to the ecommerce
    product page for some balls inline too") -- on `product` and on each
    `comparison_table` row, resolves to a REAL BowlerDepot storefront
    product-page URL when one is known, else null. Deliberately reuses
    014/016's existing price-tracking data instead of adding any new
    migration/column: price_checker's BigCommerce ('api'/'bigcommerce')
    source already resolves and stores exactly this (see price_checker.
    extract_bigcommerce_price_fields's own docstring on custom_url.url +
    base_url), once that product has an approved+active BowlerDepot price
    source. "For some balls" in Al's own phrasing is exactly right: this
    is null for any product price_checker hasn't matched/approved yet, and
    the frontend is expected to fall back to bowlerDepotSearchUrl() in
    that case, same as it already does for every ball today.

    related_reviews (Al: "add cross linking at the bottom to 'related'
    ball reviews") -- unlike comparison_table (every published sibling,
    regardless of whether it has its own article), this is filtered down
    to siblings that have their OWN approved product_articles row, since
    only those actually resolve to a real Learn article page to link to.
    Same "silently drop, don't error" posture as comparison_table for a
    sibling that's since been unpublished.

    reviewed_at (Al: "add all the proper google structured data to the
    markup" for the Learn article pages) -- newly selected here (was
    already used by list_articles' own sort, just never returned from
    THIS function) specifically to back Article structured data's
    datePublished/dateModified in scripts/prerender.ts: the actual admin-
    approval timestamp is the honest "this review was published" moment,
    not `generated_at` (when the AI draft was first produced, which can
    sit for a while in 'pending' before an admin ever approves it).

    ecommerce_price/ecommerce_price_currency/ecommerce_in_stock (same
    structured-data ask) -- real `Offer` data for `product`'s nested
    schema.org `Product`, pulled from the SAME price_checker BigCommerce
    source `ecommerce_url` already resolves (see that field's own
    docstring above), specifically the most recent successful (non-null
    price) `product_price_history` row for that source. All three are
    null together whenever `ecommerce_url` is null (no approved+active
    BowlerDepot price source yet) or price_checker's checks for that
    source have never yet produced a real price -- Google's Product
    structured data guidance is explicit that `offers`/`review`/
    `aggregateRating` must each be genuine, so this is never fabricated
    or defaulted; the JSON-LD in prerender.ts omits the whole `offers`
    block rather than guess.

    `comparison_table` rows now carry the SAME three fields (Al: "include
    links and pricing for [similar balls] using the bowlerdepot.com
    pricing data") -- originally scoped out of comparison_table (Al's
    first ask there was just inline links), but the follow-up ask is
    explicitly for real BowlerDepot pricing on that list too, not just a
    link. Same LATERAL-join shape as `product`'s own price fields above
    (a sibling's ecommerce_url and its price/currency/in_stock must come
    from the SAME chosen price source, not two independent lookups that
    could disagree), and the same all-null-together/never-fabricated
    posture: a sibling price_checker hasn't matched or has never
    successfully checked simply shows no price, same as `product` already
    does. Still not rendered into scripts/prerender.ts's static HTML --
    comparison_table's links are external BowlerDepot storefront links
    (or search-page fallbacks), not internal review-to-review links, so
    they stay out of the prerendered crawl-relevant markup for the same
    reason related_reviews' own links ARE prerendered and these never
    were (see that field's docstring).

    category_name/category_slug/article_type_name/article_type_slug
    (migration 031) -- left-joined off the article's own persisted
    category_id/article_type_id, same "Bowling Balls" / "Ball Review"
    taxonomy list_articles/list_categories expose. Null together for a
    pre-migration article that predates the backfill or a not-yet-
    onboarded product_type -- the Learn detail page should treat that the
    same as any other optional label (omit it), not an error.

    product.status ('current' or 'retired', same products.status column
    get_product already exposes) -- newly selected here specifically to
    back the Learn detail page's post-verdict "shop this ball" CTA: Al
    wants that CTA to only render while the ball is still sold, and
    retired balls (which still keep their article, per this whole
    function's "never regenerate over a spec correction" posture) simply
    shouldn't be pushed to an ecommerce page that no longer sells them.

    brand_lineup (Al: "a other balls from the same manufacture carousel
    to the bottom of each article and include current balls sorted by
    price high to low") -- every OTHER published, CURRENT product sharing
    this article's product.brand_id, with the same real BowlerDepot
    price/url fields as comparison_table, ordered by ecommerce_price
    descending (unpriced balls sort last, never first or omitted). See
    that block's own comment for why this is a fresh brand_id query
    rather than reusing sibling_product_ids like comparison_table/
    related_reviews. Empty list (not null) when the product has no
    brand_id or no other current siblings."""
    with conn.cursor() as cur:
        cur.execute("select id from products where id = %s and published = true", (product_id,))
        if cur.fetchone() is None:
            return None

        cur.execute(
            """
            select pa.id, pa.title, pa.hook, pa.performance_summary, pa.who_should_buy, pa.who_should_skip,
                   pa.pros, pa.cons, pa.buying_tips, pa.verdict, pa.faq, pa.sibling_product_ids,
                   pa.source_video_ids, pa.generated_at, pa.reviewed_at,
                   pa.action_shot_image_url, pa.product_shot_image_url,
                   cat.name as category_name, cat.slug as category_slug,
                   atype.name as article_type_name, atype.slug as article_type_slug
            from product_articles pa
            left join categories cat on cat.id = pa.category_id
            left join article_types atype on atype.id = pa.article_type_id
            where pa.product_id = %s and pa.status = 'approved'
            """,
            (product_id,),
        )
        row = cur.fetchone()
        if row is None:
            return {"product_id": product_id, "article": None}

        columns = [desc[0] for desc in cur.description]
        article = dict(zip(columns, row))

        # Live spec highlight for THIS product (name/url/image/core/
        # coverstock/per-weight SKU specs) -- same join shape as
        # get_product above, just the subset a spec-highlight/comparison
        # table section actually needs, not the full detail-page payload.
        cur.execute(
            """
            select p.name, p.url, p.status, c.name as core_name, c.core_type,
                   p.coverstock_name, p.coverstock_type, b.id as brand_id, b.name as brand_name,
                   coalesce(
                       (
                           select pi.stored_url from product_images pi
                           where pi.product_id = p.id and pi.is_visible = true
                           order by pi.is_thumbnail desc, pi.display_order, pi.id
                           limit 1
                       ),
                       p.primary_image_url
                   ) as primary_image_url,
                   ecom_source.product_url as ecommerce_url,
                   ecom_price.price as ecommerce_price,
                   ecom_price.currency as ecommerce_price_currency,
                   ecom_price.in_stock as ecommerce_in_stock
            from products p
            left join cores c on c.id = p.core_id
            left join brands b on b.id = p.brand_id
            -- Two LATERALs, not one scalar subquery per field: ecom_price
            -- must read the price_history row for the SAME price_source
            -- ecom_source picked, not just "whichever bigcommerce source
            -- happens to have the newest price check" -- see this
            -- function's own docstring on ecommerce_price/_currency/
            -- _in_stock for why that consistency matters here.
            left join lateral (
                select pps.id, pps.product_url
                from product_price_sources pps
                join price_sites ps on ps.id = pps.price_site_id
                where pps.product_id = p.id
                  and ps.api_provider = 'bigcommerce'
                  and pps.status = 'approved'
                  and pps.is_active = true
                order by pps.last_checked_at desc nulls last, pps.id
                limit 1
            ) ecom_source on true
            left join lateral (
                select price, currency, in_stock
                from product_price_history
                where price_source_id = ecom_source.id and price is not null
                order by checked_at desc
                limit 1
            ) ecom_price on true
            where p.id = %s
            """,
            (product_id,),
        )
        spec_row = cur.fetchone()
        spec_columns = [desc[0] for desc in cur.description]
        article["product"] = dict(zip(spec_columns, spec_row)) if spec_row else None

        cur.execute(
            """
            select weight_lbs, rg, differential, mass_bias
            from product_skus
            where product_id = %s
            order by weight_lbs desc
            """,
            (product_id,),
        )
        sku_columns = [desc[0] for desc in cur.description]
        skus = [dict(zip(sku_columns, r)) for r in cur.fetchall()]
        if article["product"] is not None:
            article["product"]["skus"] = skus

        # Comparison table -- see this function's own docstring for why
        # this is a fresh live join, not a stored blob. ::uuid[] cast is
        # required (sibling_product_ids comes back from jsonb as a plain
        # Python list of strings, and psycopg2's list->ARRAY adaptation
        # defaults to text[], which won't compare against a uuid column
        # without an explicit cast).
        sibling_ids = article.get("sibling_product_ids") or []
        comparison_table = []
        if sibling_ids:
            cur.execute(
                """
                select p.id, p.name, p.url, c.name as core_name,
                       p.coverstock_name,
                       coalesce(
                           (
                               select pi.stored_url from product_images pi
                               where pi.product_id = p.id and pi.is_visible = true
                               order by pi.is_thumbnail desc, pi.display_order, pi.id
                               limit 1
                           ),
                           p.primary_image_url
                       ) as primary_image_url,
                       ecom_source.product_url as ecommerce_url,
                       ecom_price.price as ecommerce_price,
                       ecom_price.currency as ecommerce_price_currency,
                       ecom_price.in_stock as ecommerce_in_stock
                from products p
                left join cores c on c.id = p.core_id
                -- Same two-LATERAL shape as the `product` spec_row query
                -- above (and same reasoning: ecom_price must read the
                -- SAME price_source ecom_source picked for THIS sibling).
                left join lateral (
                    select pps.id, pps.product_url
                    from product_price_sources pps
                    join price_sites ps on ps.id = pps.price_site_id
                    where pps.product_id = p.id
                      and ps.api_provider = 'bigcommerce'
                      and pps.status = 'approved'
                      and pps.is_active = true
                    order by pps.last_checked_at desc nulls last, pps.id
                    limit 1
                ) ecom_source on true
                left join lateral (
                    select price, currency, in_stock
                    from product_price_history
                    where price_source_id = ecom_source.id and price is not null
                    order by checked_at desc
                    limit 1
                ) ecom_price on true
                where p.id = any(%s::uuid[]) and p.published = true
                order by p.name
                """,
                (sibling_ids,),
            )
            sib_columns = [desc[0] for desc in cur.description]
            comparison_table = [dict(zip(sib_columns, r)) for r in cur.fetchall()]
        article["comparison_table"] = comparison_table

        # Related reviews (Al: "cross linking at the bottom to 'related'
        # ball reviews") -- same sibling_product_ids source as
        # comparison_table above, but narrowed to siblings that have their
        # OWN approved article to actually link to (an inner join on
        # product_articles does this narrowing for free). Ordered
        # newest-reviewed-first, same convention list_articles' default
        # sort uses, so the freshest related content surfaces first.
        related_reviews = []
        if sibling_ids:
            cur.execute(
                """
                select p.id as product_id, p.name as product_name,
                       pa.id as article_id, pa.title, pa.hook, pa.reviewed_at,
                       coalesce(
                           (
                               select pi.stored_url from product_images pi
                               where pi.product_id = p.id and pi.is_visible = true
                               order by pi.is_thumbnail desc, pi.display_order, pi.id
                               limit 1
                           ),
                           p.primary_image_url
                       ) as primary_image_url
                from products p
                join product_articles pa on pa.product_id = p.id and pa.status = 'approved'
                where p.id = any(%s::uuid[]) and p.published = true
                order by pa.reviewed_at desc nulls last, p.name
                """,
                (sibling_ids,),
            )
            rel_columns = [desc[0] for desc in cur.description]
            related_reviews = [dict(zip(rel_columns, r)) for r in cur.fetchall()]
        article["related_reviews"] = related_reviews

        # Brand lineup carousel (Al: "add a other balls from the same
        # manufacture carousel to the bottom of each article and include
        # current balls sorted by price high to low"). Deliberately NOT
        # sibling_product_ids-based like comparison_table/related_reviews
        # above -- this is meant to be the WHOLE current lineup for the
        # brand, not just the heuristic handful of specs/core-family
        # matches product_article_generator picked when the article was
        # written, so it's a fresh brand_id query instead. status =
        # 'current' only (same reasoning as the shop-this-ball CTA's own
        # product.status gate): a retired sibling isn't for sale, so it
        # doesn't belong in a "shop more from this brand" rail. Same
        # LATERAL price-join shape as comparison_table for the same
        # consistency reason (a ball's ecommerce_url and its price must
        # come from the SAME chosen price source). Ordered by price
        # descending as asked, unpriced balls (price_checker hasn't
        # matched/checked them yet) sort last rather than first or
        # erroring the whole rail.
        brand_lineup = []
        brand_id = article["product"].get("brand_id") if article["product"] else None
        if brand_id:
            cur.execute(
                """
                select p.id, p.name, p.url, c.name as core_name,
                       p.coverstock_name,
                       coalesce(
                           (
                               select pi.stored_url from product_images pi
                               where pi.product_id = p.id and pi.is_visible = true
                               order by pi.is_thumbnail desc, pi.display_order, pi.id
                               limit 1
                           ),
                           p.primary_image_url
                       ) as primary_image_url,
                       ecom_source.product_url as ecommerce_url,
                       ecom_price.price as ecommerce_price,
                       ecom_price.currency as ecommerce_price_currency,
                       ecom_price.in_stock as ecommerce_in_stock
                from products p
                left join cores c on c.id = p.core_id
                left join lateral (
                    select pps.id, pps.product_url
                    from product_price_sources pps
                    join price_sites ps on ps.id = pps.price_site_id
                    where pps.product_id = p.id
                      and ps.api_provider = 'bigcommerce'
                      and pps.status = 'approved'
                      and pps.is_active = true
                    order by pps.last_checked_at desc nulls last, pps.id
                    limit 1
                ) ecom_source on true
                left join lateral (
                    select price, currency, in_stock
                    from product_price_history
                    where price_source_id = ecom_source.id and price is not null
                    order by checked_at desc
                    limit 1
                ) ecom_price on true
                where p.brand_id = %s and p.status = 'current' and p.published = true and p.id != %s
                order by ecom_price.price desc nulls last, p.name
                limit 30
                """,
                (brand_id, product_id),
            )
            brand_columns = [desc[0] for desc in cur.description]
            brand_lineup = [dict(zip(brand_columns, r)) for r in cur.fetchall()]
        article["brand_lineup"] = brand_lineup

        return {"product_id": product_id, "article": article}


def get_products_compare(conn, ids: list) -> list:
    """Batch fetch for the comparison page -- Al's ask for "an intuitive
    way to populate a ball comparison page" needs the frontend to be able
    to add/remove balls to a comparison set and see them all at once
    without an extra round-trip per ball. Reuses get_product's exact
    per-product shape (skus/images/videos and all) so the comparison page
    and the detail page can share one rendering component for a single
    ball's data.

    Capped at MAX_COMPARE_IDS -- a comparison grid wide enough to need
    more than that isn't usable UI regardless of what the backend could
    technically return, so the cap is enforced here rather than left to
    the frontend's judgment. Missing or unpublished ids are silently
    dropped (not an error) -- same reasoning as a single get_product
    returning None rather than distinguishing "doesn't exist" from
    "unpublished": the caller gets back whatever subset is real and
    public, not a 404 for the whole batch over one bad id. Preserves the
    input id order for whatever DID resolve, so the frontend's comparison
    columns stay in the order the visitor picked them."""
    ids = ids[:MAX_COMPARE_IDS]
    by_id = {}
    for product_id in ids:
        product = get_product(conn, product_id)
        if product is not None:
            by_id[product_id] = product
    return [by_id[i] for i in ids if i in by_id]


MAX_COMPARE_IDS = 6


# --------------------------------------------------------------------
# Retired -> current similarity suggestions
# --------------------------------------------------------------------

# Normalization constants for RG/DIFF, so a raw distance calculation
# doesn't let one spec dominate the other just because of its natural
# unit scale -- RG typically spans roughly 2.46-2.80 (a range of ~0.34),
# DIFF typically spans roughly 0.010-0.065 (a range of ~0.055). Dividing
# each spec's raw difference by its own typical range before combining
# puts both specs on a comparable ~0-1 scale. These are round, documented
# starting-point constants (not fit against real data -- there's no
# labeled "these two balls actually play alike" dataset to fit against),
# meant to be revisited once Al's existing plotter (mentioned when this
# feature was requested: "I have already created an interactive bowling
# ball plotter... in another cowork project") is wired in -- if that tool
# already has its own notion of ball-motion distance, prefer reusing that
# over this heuristic rather than running two different "how similar are
# these balls" answers on the same site.
RG_RANGE = 0.35
DIFF_RANGE = 0.06

# Categorical mismatch penalties, same normalized ~0-1 scale as the RG/
# DIFF distance above so they combine sensibly. Core type (symmetric vs.
# asymmetric) drives more of a ball's overall motion character than
# coverstock does, hence the larger penalty -- coverstock type/material
# mismatches still matter (a solid reactive plays very differently from a
# pearl even with an identical core) but are weighted lower.
CORE_TYPE_MISMATCH_PENALTY = 0.5
COVERSTOCK_TYPE_MISMATCH_PENALTY = 0.2
COVERSTOCK_MATERIAL_MISMATCH_PENALTY = 0.2


def _reference_sku(skus: list):
    """Picks the one SKU that best represents a product's overall RG/DIFF
    for similarity scoring -- mirrors 001_init_schema.sql's own stated
    convention ("when a source gives only one RG/DIFF value... that value
    is the 15 lb ball"): prefer the real 15lb row if present, otherwise
    the row closest to 15lb, otherwise (no skus at all) None. skus is
    expected pre-filtered to rows with a non-null rg/differential --
    callers do that filtering before calling this, since "no usable spec
    data" is a real, valid state or two different collections."""
    if not skus:
        return None
    for sku in skus:
        if sku["weight_lbs"] == 15:
            return sku
    return min(skus, key=lambda s: abs(s["weight_lbs"] - 15))


def score_similarity(source: dict, candidate: dict) -> float:
    """Pure scoring function, kept separate from the DB-fetching code
    below so it's directly unit-testable against hand-built fixtures
    without a fake connection. Lower score = more similar; source/
    candidate are both dicts with rg/differential (from _reference_sku)
    plus core_type/coverstock_type/coverstock_material. A None rg/
    differential on either side skips the numeric-distance term entirely
    (adds 0, not a penalty) rather than crashing or silently treating a
    missing spec as a huge mismatch -- categorical terms still apply."""
    score = 0.0

    if source.get("rg") is not None and candidate.get("rg") is not None:
        score += (abs(float(source["rg"]) - float(candidate["rg"])) / RG_RANGE) ** 2
    if source.get("differential") is not None and candidate.get("differential") is not None:
        score += (abs(float(source["differential"]) - float(candidate["differential"])) / DIFF_RANGE) ** 2
    score = score ** 0.5

    if source.get("core_type") and candidate.get("core_type") and source["core_type"] != candidate["core_type"]:
        score += CORE_TYPE_MISMATCH_PENALTY
    if source.get("coverstock_type") and candidate.get("coverstock_type") and source["coverstock_type"] != candidate["coverstock_type"]:
        score += COVERSTOCK_TYPE_MISMATCH_PENALTY
    if source.get("coverstock_material") and candidate.get("coverstock_material") and source["coverstock_material"] != candidate["coverstock_material"]:
        score += COVERSTOCK_MATERIAL_MISMATCH_PENALTY

    return score


def list_similar_products(conn, product_id: str, limit: int = 5) -> list:
    """The other half of Al's ask: "suggest current balls that best
    compare to the retired balls". Works for any product (not just
    retired ones -- a current ball's detail page showing "similar balls"
    is a reasonable feature too, and nothing here assumes the source is
    retired), but candidates are always restricted to published,
    status='current' products excluding the source itself -- the point
    is specifically to route a visitor looking at something no longer
    sold toward something they actually can buy today.

    Returns [] (not None, not an error) if the source id doesn't exist,
    isn't published, or has no usable RG/DIFF on any weight -- app.py
    treats an empty list as a normal, renderable ("no close matches
    found") response, not a 404, since the product detail page itself
    already 404s independently via get_product if the id is genuinely
    bad.

    Scoring happens in Python, not SQL, on purpose: the published current
    catalog is at most a few hundred products (nowhere near enough to
    need this to run as a database-side computation), and score_similarity
    being a plain, independently-unit-tested function is worth more here
    than a harder-to-verify SQL expression would be."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select p.id, c.core_type, p.coverstock_type, p.coverstock_material
            from products p
            left join cores c on c.id = p.core_id
            where p.id = %s and p.published = true
            """,
            (product_id,),
        )
        source_row = cur.fetchone()
        if source_row is None:
            return []
        source_columns = [desc[0] for desc in cur.description]
        source = dict(zip(source_columns, source_row))

        cur.execute(
            "select weight_lbs, rg, differential from product_skus where product_id = %s and rg is not null",
            (product_id,),
        )
        sku_columns = [desc[0] for desc in cur.description]
        source_skus = [dict(zip(sku_columns, row)) for row in cur.fetchall()]
        source_sku = _reference_sku(source_skus)
        source["rg"] = source_sku["rg"] if source_sku else None
        source["differential"] = source_sku["differential"] if source_sku else None

        cur.execute(
            """
            select p.id, p.name, p.url, p.color,
                   b.name as brand_name,
                   c.core_type, p.coverstock_type, p.coverstock_material, p.coverstock_name,
                   coalesce(
                       (
                           select pi.stored_url from product_images pi
                           where pi.product_id = p.id and pi.is_visible = true
                           order by pi.is_thumbnail desc, pi.display_order, pi.id
                           limit 1
                       ),
                       p.primary_image_url
                   ) as primary_image_url
            from products p
            join brands b on b.id = p.brand_id
            left join cores c on c.id = p.core_id
            where p.published = true and p.status = 'current' and p.id != %s
            """,
            (product_id,),
        )
        candidate_columns = [desc[0] for desc in cur.description]
        candidates = [dict(zip(candidate_columns, row)) for row in cur.fetchall()]

        candidate_ids = [c["id"] for c in candidates]
        skus_by_product = {}
        if candidate_ids:
            cur.execute(
                "select product_id, weight_lbs, rg, differential from product_skus "
                "where product_id = any(%s::uuid[]) and rg is not null",
                (candidate_ids,),
            )
            sku_columns2 = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                sku = dict(zip(sku_columns2, row))
                skus_by_product.setdefault(sku["product_id"], []).append(sku)

    scored = []
    for candidate in candidates:
        candidate_sku = _reference_sku(skus_by_product.get(candidate["id"], []))
        candidate["rg"] = candidate_sku["rg"] if candidate_sku else None
        candidate["differential"] = candidate_sku["differential"] if candidate_sku else None
        score = score_similarity(source, candidate)
        scored.append((score, candidate))

    scored.sort(key=lambda pair: pair[0])
    results = []
    for score, candidate in scored[:limit]:
        candidate["similarity_score"] = round(score, 4)
        results.append(candidate)
    return results


# --------------------------------------------------------------------
# Ball motion plotter (Al's existing tool, integrated as a standalone
# page -- see products.oil_rating/motion_rating's migration 011 and this
# module's own header comment for the full backstory)
# --------------------------------------------------------------------

# estimate_oil_motion is a documented heuristic -- same spirit and same
# caveat as RG_RANGE/DIFF_RANGE above, still a small linear model over
# core/coverstock features, NOT a real physics simulation. It STARTED as
# pure general bowling-industry domain knowledge (see the original
# reasoning paragraphs below, kept for context), but as of 2026-08-14 its
# constants are REFIT against real data: Al's own reported experience
# ("i feel like it is way off for most balls") plus the 2026-08-12
# spot-check (see DEPLOY_RUNBOOK.md 6m) showed the original domain-
# knowledge-only constants had real, systematic misses -- confirmed once
# scripts/dump_plotter_estimate_training_data.py pulled the real (core/
# coverstock inputs -> actual chart oil/motion) pairs for all 40 products
# that were, at the time, matched onto a real Brunswick chart position
# (oil_motion_source='chart').
#
# Original domain-knowledge reasoning (still directionally true, still
# why each axis uses the inputs it does -- only the exact numbers below
# changed):
#
#   oil (1 light -> 16 heavy) is primarily a COVERSTOCK friction/traction
#   question -- higher-friction covers hook earlier and need more oil on
#   the lane to be controllable, lower-friction covers skid further and
#   suit lighter/drier conditions. Material dominates (polyester <
#   urethane < reactive resin), and within reactive resin, type matters
#   too (pearl skids more than solid). Particle coverstocks push further
#   into heavy-oil territory than any of those alone.
#
#   motion (1 smooth -> 18 angular) is primarily a CORE question --
#   asymmetric cores create a sharper, more defined direction change than
#   symmetric ones, and that effect scales with differential (more flare
#   potential = more angular). Coverstock type gets a smaller secondary
#   nudge.
#
# WHAT THE REAL DATA ACTUALLY SHOWED (40 chart-matched products,
# 2026-08-14 refit -- see scripts/dump_plotter_estimate_training_data.py
# and its own module docstring for how this was pulled):
#
#   The single biggest miss, by far: OIL_ADJUST_BY_TYPE's old flat "+3"
#   for a solid coverstock. Real reactive-resin/solid balls (n=16, the
#   single largest group in the data) average oil=10.0 -- essentially
#   IDENTICAL to reactive-resin/hybrid's own real average (9.4), not 3
#   points heavier. The old +3 overshot this whole class hard (e.g.
#   Revenge Solid: real oil 3, old estimate 13) -- exactly the pattern
#   DEPLOY_RUNBOOK.md's 2026-08-12 spot-check flagged. Fixed by dropping
#   solid's oil adjustment to 0 (same as hybrid). Note this group is also
#   the model's biggest remaining known weakness: real oil for reactive-
#   resin/solid balls genuinely ranges from 3 (Revenge Solid) to 16 (Zero
#   Mercy Solid) even holding material+type fixed -- a real, wide spread
#   this 2-input model structurally can't capture. Worth a future revisit
#   with a more granular input (e.g. which core LINE a ball belongs to)
#   once that's available as structured data, not just a bigger version
#   of this same formula.
#
#   Urethane's oil base nudged 5 -> 6 (real urethane balls average 6.0
#   across the 5 samples available -- still a small sample, still worth
#   more data over time).
#
#   Motion's real numbers ran higher across the board than the original
#   guesses at every core-type base AND needed a stronger differential
#   weight to match -- refit via ordinary least squares against all 40
#   points (course inputs: core_type dummy, coverstock_type dummy,
#   differential). One genuine surprise vs. the original hand-written
#   reasoning: real solid-coverstock balls trend slightly MORE angular
#   than hybrid, not less (the old "-1" was backwards; real data wants
#   roughly "+1") -- pearl's real "more angular than hybrid" direction
#   held up (old +1 was directionally right, just too small).
#
#   OIL_PARTICLE_BONUS and OIL_BASE_BY_MATERIAL["polyester_plastic"]
#   could NOT be refit -- zero has_particle=true or polyester_plastic
#   products exist in this 40-product real-chart dataset, so both are
#   still the original, untested domain-knowledge guesses.
#
#   Measured accuracy, old vs. new formula, both scored against the same
#   40 real chart positions: oil mean absolute error 3.05 -> 2.675 (on
#   the 1-16 scale), exact matches 4/40 -> 4/40 (unchanged), within +/-2
#   18/40 -> 18/40 (unchanged, oil's real spread inside the solid group
#   above is the limiting factor, not the constants); motion mean
#   absolute error 2.75 -> 2.5 (on the 1-18 scale), exact matches 3/40 ->
#   6/40, within +/-2 22/40 -> 24/40. A real, modest, net improvement
#   across the board (no metric regressed) -- not a dramatic fix, because
#   real ball motion depends on more than these few inputs, but a
#   genuine step up validated against real answers instead of guessed a
#   second time.
#
# scripts/reestimate_plotter_positions.py (+ admin_api.reestimate_
# plotter_positions) re-runs THIS formula against every product still
# marked oil_motion_source='estimated' so products estimated under the
# OLD constants actually get the fix, not just new ones.
#
# Revisit again once more real chart/reference data exists -- especially
# the reactive-resin/solid spread flagged above, and OIL_PARTICLE_BONUS/
# polyester_plastic once a real particle or plastic-cover chart match
# shows up.

OIL_BASE_BY_MATERIAL = {
    "polyester_plastic": 2,   # unchanged -- no real polyester_plastic samples to refit against
    "urethane": 6,
    "reactive_resin": 10,
}
OIL_ADJUST_BY_TYPE = {
    "pearl": -3,   # unchanged -- matched real data closely already
    "hybrid": 0,
    "solid": 0,    # was +3 -- the single biggest fix, see comment above
}
OIL_PARTICLE_BONUS = 2  # unchanged -- no real has_particle=true samples to refit against

MOTION_BASE_BY_CORE_TYPE = {
    "symmetric": 4,
    "asymmetric": 8,
}
MOTION_BASE_UNKNOWN_CORE = 6  # default when core_type is unset
MOTION_DIFF_MIDPOINT = 0.02   # unchanged -- still roughly the low end of a typical differential range
MOTION_DIFF_SCALE = 0.045     # unchanged -- still roughly the typical differential range's span
MOTION_DIFF_WEIGHT = 8        # how many motion points a full-range differential swing is worth
MOTION_ADJUST_BY_COVERSTOCK_TYPE = {
    "pearl": 2,
    "solid": 1,    # was -1 -- real data runs the opposite direction from the original guess, see comment above
    "hybrid": 0,
}

OIL_MIN, OIL_MAX = 1, 16
MOTION_MIN, MOTION_MAX = 1, 18


def _clamp(value: float, low: int, high: int) -> int:
    return max(low, min(high, round(value)))


def estimate_oil_motion(core_type: str = None, coverstock_type: str = None,
                         coverstock_material: str = None, has_particle: bool = False,
                         differential: float = None) -> dict:
    """Pure function (no DB) -- see the module-level comment above this
    for the reasoning behind every constant here. Always returns a
    usable (oil, motion) pair, even with every input missing (falls back
    to the middle of each axis) -- a product this sparse is rare (every
    scraper always writes coverstock_material/coverstock_type at
    minimum) but the frontend shouldn't have to special-case a missing
    plotter position for a published, presumably-real product."""
    oil = OIL_BASE_BY_MATERIAL.get(coverstock_material, (OIL_MIN + OIL_MAX) / 2)
    oil += OIL_ADJUST_BY_TYPE.get(coverstock_type, 0)
    if has_particle:
        oil += OIL_PARTICLE_BONUS
    oil = _clamp(oil, OIL_MIN, OIL_MAX)

    motion = MOTION_BASE_BY_CORE_TYPE.get(core_type, MOTION_BASE_UNKNOWN_CORE)
    if differential is not None:
        motion += ((float(differential) - MOTION_DIFF_MIDPOINT) / MOTION_DIFF_SCALE) * MOTION_DIFF_WEIGHT
    motion += MOTION_ADJUST_BY_COVERSTOCK_TYPE.get(coverstock_type, 0)
    motion = _clamp(motion, MOTION_MIN, MOTION_MAX)

    return {"oil": oil, "motion": motion}


def list_plotter_positions(conn, status: str = "current", ids: list = None) -> list:
    """Everything the standalone plotter page needs in one unpaginated
    call -- Al's original tool (see this module's header comment) loaded
    its whole 56-ball dataset up front rather than paginating, and the
    published current catalog here is a similar order of magnitude (a
    few hundred products at most), so the same shape carries over rather
    than adding pagination this page doesn't need.

    oil_rating/motion_rating/oil_motion_source (migrations 011/012) are
    READ here, not computed -- Al's own direct follow-up after this
    function originally called estimate_oil_motion live on every request:
    "it will cause for potential inconsistencies, i would prefer for it
    to just back fill the values once in the DB and then estimate on
    scrape if not set". The persisted value now comes from one of three
    places: a chart match (scripts/backfill_plotter_chart_positions.py,
    oil_motion_source='chart'), an estimate written automatically the
    first time a product was scraped with no position yet (every
    upsert_product across all five scraper Lambdas, 'estimated'), or an
    admin's manual correction (PATCH /products/{id}/plotter-position,
    'manual'). estimate_oil_motion is still called below, but ONLY as a
    last-resort defensive fallback for a product that genuinely has
    neither -- predates this whole feature and hasn't been rescraped or
    covered by admin_api.backfill_estimated_plotter_positions yet. That
    fallback value is intentionally never written back to the row here
    (this module has no write access by design -- see this file's own
    header comment); it just keeps the plotter page from ever silently
    dropping a product, until a real backfill/rescrape lands a persisted
    value for it.

    ids (optional): when given, returns positions for exactly this set of
    product ids instead of the whole status-filtered catalog -- backs the
    plotter page's "Compare" tab (Al: "add a tablist toggle to the ball
    motion plotter that is 'compare' and plots the currently selected
    balls in the compare feature"). The compare list is arbitrary ids a
    visitor picked while browsing, not necessarily all the same status,
    so `status` is ignored entirely when `ids` is given -- same contract
    as get_products_compare above (capped at MAX_COMPARE_IDS, missing or
    unpublished ids silently dropped rather than erroring, input id order
    preserved on the way out so the two features' compare sets stay in
    the same visible order)."""
    ids = ids[:MAX_COMPARE_IDS] if ids else None
    with conn.cursor() as cur:
        if ids:
            where_clause = "p.published = true and p.id = any(%s::uuid[])"
            params = (ids,)
        else:
            where_clause = "p.published = true and p.status = %s"
            params = (status,)
        cur.execute(
            f"""
            select p.id, p.name, p.url,
                   b.name as brand_name,
                   c.core_type, p.coverstock_type, p.coverstock_material, p.has_particle,
                   p.oil_rating, p.motion_rating, p.oil_motion_source,
                   coalesce(
                       (
                           select pi.stored_url from product_images pi
                           where pi.product_id = p.id and pi.is_visible = true
                           order by pi.is_thumbnail desc, pi.display_order, pi.id
                           limit 1
                       ),
                       p.primary_image_url
                   ) as primary_image_url
            from products p
            join brands b on b.id = p.brand_id
            left join cores c on c.id = p.core_id
            where {where_clause}
            """,
            params,
        )
        columns = [desc[0] for desc in cur.description]
        products = [dict(zip(columns, row)) for row in cur.fetchall()]

        product_ids = [p["id"] for p in products]
        skus_by_product = {}
        if product_ids:
            cur.execute(
                "select product_id, weight_lbs, rg, differential from product_skus "
                "where product_id = any(%s::uuid[]) and differential is not null",
                (product_ids,),
            )
            sku_columns = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                sku = dict(zip(sku_columns, row))
                skus_by_product.setdefault(sku["product_id"], []).append(sku)

    results = []
    for p in products:
        if p["oil_rating"] is not None and p["motion_rating"] is not None:
            oil, motion, source = p["oil_rating"], p["motion_rating"], p["oil_motion_source"] or "estimated"
        else:
            ref_sku = _reference_sku(skus_by_product.get(p["id"], []))
            estimate = estimate_oil_motion(
                core_type=p["core_type"], coverstock_type=p["coverstock_type"],
                coverstock_material=p["coverstock_material"], has_particle=p["has_particle"],
                differential=ref_sku["differential"] if ref_sku else None,
            )
            oil, motion, source = estimate["oil"], estimate["motion"], "estimated"
        results.append({
            "id": p["id"], "name": p["name"], "url": p["url"], "brand_name": p["brand_name"],
            "primary_image_url": p["primary_image_url"],
            "oil": oil, "motion": motion, "oil_motion_source": source,
        })

    if ids:
        # Preserve the caller's own ordering (same reasoning as
        # get_products_compare) -- an id that didn't resolve (unknown or
        # unpublished) is silently dropped rather than erroring.
        by_id = {r["id"]: r for r in results}
        results = [by_id[i] for i in ids if i in by_id]

    return results
