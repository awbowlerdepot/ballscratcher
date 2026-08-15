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
# is the next best thing." There's no point-in-time view-count HISTORY to
# work with (stats_fetched_at, migration 013, only ever holds the latest
# fetch) -- no way to measure real view *velocity*. This is a deliberate
# proxy instead: exponential decay by each video's own age (published_at),
# same half-life shape as radioactive decay -- a video's view_count counts
# for half as much once it's POPULARITY_HALF_LIFE_DAYS old, a quarter at
# 2x that, an eighth at 3x, and so on, asymptotically toward (never
# reaching) zero.
#
# HALF_LIFE_DAYS=180 (6 months), not the more conventional 12-month
# default for this kind of ranking -- Al's own follow-up question: "bowling
# balls ... are usually retired in 6-12 months, do you think that has an
# effect on this recommendation." It does: list_products defaults to
# status='current', and a current ball's entire video history almost
# always sits inside that same 6-12 month window. A 12- or 24-month
# half-life barely decays anything across a window that short -- it would
# rank current balls almost entirely by raw view count, exactly what this
# feature exists to avoid. A 6-month half-life gives real separation
# inside a ball's actual current lifespan: a launch-month video still
# counts for roughly half by the time that same ball nears retirement,
# instead of the decay curve being nearly flat the whole time.
#
# Interpolated directly into the SQL text below (not passed as a bind
# param) -- it's a fixed Python constant, not caller input, so there's no
# injection concern, and keeping it out of params avoids shifting every
# other bind param's position in this already-parameter-heavy query. Kept
# in sync by hand with admin_api/service.py's identical copy of this same
# constant/subquery -- these are two independently-deployed Lambdas with
# no shared module between them (see this project's other per-Lambda
# duplicated constants, e.g. MAX_VIDEO_IDS_PER_CALL).
POPULARITY_HALF_LIFE_DAYS = 180

# NULLs (never-fetched view_count, or a video with no published_at/
# created_at at all -- shouldn't happen, created_at has a NOT NULL
# default, but coalesce guards it anyway) are excluded via `pv.view_count
# is not null` rather than treated as 0 -- a video nobody's pulled stats
# for yet should be invisible to this ranking, not silently count as "0
# views" and drag a ball's score down before a stats refresh ever runs.
# Only status='approved' rows count (Al's confirmed choice) -- an
# unreviewed 'pending' candidate might not even really be about this
# product yet (see reassign_video_candidate's whole reason for existing).
#
# AVERAGE decayed view count per video, times ln(1 + video count) --
# NOT a plain sum. Al's follow-up, real incident: a raw sum (the
# original shape here) let video COUNT dominate the ranking -- a ball
# with 20 mediocre videos could outrank a ball with 4 genuinely popular
# ones purely on volume, which isn't "popular", it's "reviewed a lot."
# ln(1 + count) still gives volume a real, deliberate boost (more
# corroborating videos IS meaningfully more evidence of popularity than
# fewer), just a sub-linear one instead of a straight multiplier: at
# equal per-video quality, 20 videos score ~1.9x a 4-video ball (ln(21)
# / ln(5)), not the old 5x (20/4) a raw sum produced. A few standout
# videos can still beat a pile of average ones, since the AVERAGE is
# what's being scaled, not the count itself. count(*) is always >= 1
# whenever the WHERE clause matches any row, and the whole subquery
# returns NULL (then 0, via the outer coalesce) when it matches zero
# rows -- no separate zero-video special case needed.
_POPULARITY_SCORE_SQL = f"""coalesce((
                   select avg(
                       pv.view_count * power(2, -extract(epoch from (now() - coalesce(pv.published_at, pv.created_at))) / (86400.0 * {POPULARITY_HALF_LIFE_DAYS}))
                   ) * ln(1 + count(*))
                   from product_videos pv
                   where pv.product_id = p.id and pv.status = 'approved' and pv.view_count is not null
               ), 0)"""

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
# service.py's identical copy, same no-shared-module reasoning as
# POPULARITY_HALF_LIFE_DAYS above.
_SORT_ORDER_BY = {
    "popularity": "popularity_score desc, p.id asc",
    "newest": "p.release_date desc nulls last, p.id asc",
    "oldest": "p.release_date asc nulls last, p.id asc",
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

    popularity_score is always computed and returned (see
    _POPULARITY_SCORE_SQL/POPULARITY_HALF_LIFE_DAYS above) -- cheap
    enough at this catalog's size to include on every call, not gated
    behind sort='popularity', so a card can show a "trending" indicator
    even when the visitor is browsing in the default order.

    sort: None (default) keeps the existing 'updated_at desc' order --
    most-recently-touched-by-a-scraper first, which is really "recently
    changed", not "popular". Accepted values (see _SORT_ORDER_BY above):
    'popularity' (the view-count-decay ranking -- Al's ask, see
    _POPULARITY_SCORE_SQL's docstring), 'newest'/'oldest' (release_date),
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
               {_POPULARITY_SCORE_SQL} as popularity_score
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
