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

def list_products(conn, status: str = "current", brand_id: str = None, core_id: str = None,
                   coverstock_id: str = None, search: str = None,
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
    release_date, primary_image_url (falls back to the first visible
    product_images row's stored_url if primary_image_url itself is unset
    -- see _primary_image_subquery), and video_reviews_summary_video_
    count (so a card can show "based on N video reviews" without a
    second round-trip -- video_reviews_summary's actual TEXT is left for
    the detail page, a card has no room for it)."""
    query = """
        select p.id, p.name, p.url, p.color, p.status,
               b.name as brand_name,
               c.name as core_name, c.core_type,
               p.coverstock_name, p.coverstock_type, p.coverstock_material,
               p.release_date,
               coalesce(
                   p.primary_image_url,
                   (
                       select pi.stored_url from product_images pi
                       where pi.product_id = p.id and pi.is_visible = true
                       order by pi.display_order, pi.id
                       limit 1
                   )
               ) as primary_image_url,
               p.video_reviews_summary_video_count
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
    # sharing an updated_at/release_date value make plain OFFSET/LIMIT
    # pagination unstable once there's a real paginated consumer (the
    # Browse page) rather than a one-shot admin listing.
    query += " order by p.updated_at desc, p.id asc limit %s offset %s"
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
                   p.part_number, p.weights_available, p.usbc_approval_date,
                   p.release_date, p.description, p.status,
                   p.primary_image_url,
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
                       p.primary_image_url,
                       (
                           select pi.stored_url from product_images pi
                           where pi.product_id = p.id and pi.is_visible = true
                           order by pi.display_order, pi.id
                           limit 1
                       )
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
                "where product_id = any(%s) and rg is not null",
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
