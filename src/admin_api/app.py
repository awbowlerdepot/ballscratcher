"""
FastAPI admin approval API, deployed to Lambda behind API Gateway via
Mangum. Thin routing layer only -- every endpoint here just validates the
request shape and calls into service.py, which holds the actual logic and
is what's unit tested (see tests/test_admin_api_service.py and its header
comment for why: fastapi/pydantic weren't installable in this sandbox, so
this file's routes are untested/unverified this session -- same "logic
verified, deployment isn't" status as the other functions, just with the
line drawn one layer further out here since even the framework glue
couldn't be exercised. Review this file itself with a bit more scrutiny
before deploying than you would code that was actually run.

Implements the workflow the architecture doc decided on: list/inspect
pending review_queue rows, approve (applies the proposed value) or reject
(discards it, keeps the current value) them, and a small products listing/
publish-toggle surface for managing the `published` flag the consumer site
and BowlerDepot sync will read.
"""
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from mangum import Mangum
from pydantic import BaseModel

import service

app = FastAPI(title="Bowling Ball Scraper Admin API")


class ApproveRequest(BaseModel):
    resolved_by: str


class RejectRequest(BaseModel):
    resolved_by: str
    reason: Optional[str] = None


class PublishRequest(BaseModel):
    published: bool


class ImageUpdateRequest(BaseModel):
    # Both optional/independent -- a caller sets whichever it's actually
    # changing (see service.update_product_image's docstring). Pydantic's
    # default None for an unset Optional field is what lets service.py
    # distinguish "not provided" from "explicitly set to False".
    is_visible: Optional[bool] = None
    is_thumbnail: Optional[bool] = None


class ImageReorderRequest(BaseModel):
    image_ids: list[str]


class PlotterPositionRequest(BaseModel):
    # oil_rating/motion_rating both required, not independently-optional
    # like ImageUpdateRequest's fields -- see service.set_plotter_
    # position's docstring for why a plotter position is meaningless with
    # only one axis set. source defaults to 'manual' -- an admin correcting
    # an estimate via the admin site never has to think about this field;
    # scripts/backfill_plotter_chart_positions.py is the one caller that
    # passes 'chart' explicitly (see migration 012's header comment for
    # the three source values and what each means).
    oil_rating: int
    motion_rating: int
    source: Literal["chart", "estimated", "manual"] = "manual"


class ReassignRequest(BaseModel):
    product_id: str
    # Optional: stamped on the ORIGIN row's rejected tombstone only (see
    # service.reassign_video_candidate's docstring), not on the target --
    # same "who did this" audit field approve/reject use elsewhere.
    resolved_by: Optional[str] = None


class PriceSiteCreateRequest(BaseModel):
    name: str
    # Site-SEARCH config -- what price_checker's discovery job uses to
    # find candidate product URLs on this site (see
    # service.create_price_site's docstring). {query} in
    # search_url_template gets url-encoded and substituted by
    # price_checker.search_site_for_product. Optional now (016_price_
    # tracking_bigcommerce.sql) -- REQUIRED for fetch_method='scrape'
    # (the default) but not for 'api', enforced by the DB's own
    # price_sites_fetch_method_fields_check, not re-validated here.
    search_url_template: Optional[str] = None
    result_link_selector: Optional[str] = None
    # Price-PAGE config -- what checking uses once a candidate is approved.
    default_css_selector: Optional[str] = None
    notes: Optional[str] = None
    # 'scrape' (the original generic search+selector design) or 'api'
    # (currently only 'bigcommerce', BowlerDepot) -- see
    # service.create_price_site's docstring.
    fetch_method: str = "scrape"
    api_provider: Optional[str] = None
    base_url: Optional[str] = None


class PriceSiteUpdateRequest(BaseModel):
    # All optional/independent, same convention as ImageUpdateRequest --
    # a caller sets whichever field it's actually changing (see
    # service.update_price_site's docstring).
    name: Optional[str] = None
    search_url_template: Optional[str] = None
    result_link_selector: Optional[str] = None
    default_css_selector: Optional[str] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None
    fetch_method: Optional[str] = None
    api_provider: Optional[str] = None
    base_url: Optional[str] = None


class ProductPriceSourceCreateRequest(BaseModel):
    # Manual-override path only -- see service.create_product_price_source's
    # docstring. The normal way a product_price_sources row comes into
    # being is price_checker's discovery job finding it automatically;
    # this endpoint is for when that search missed or found the wrong URL.
    price_site_id: str
    product_url: str
    # None = use the site's own default_css_selector (see
    # service.create_product_price_source's docstring) -- only set this
    # when this one product's page needs a different selector.
    css_selector: Optional[str] = None
    resolved_by: Optional[str] = None
    # Only meaningful for a manual override against an 'api'-fetch_method
    # site (016_price_tracking_bigcommerce.sql) -- e.g. an admin manually
    # attaching a BowlerDepot product id discovery missed.
    external_product_id: Optional[str] = None


class ProductPriceSourceUpdateRequest(BaseModel):
    product_url: Optional[str] = None
    css_selector: Optional[str] = None
    is_active: Optional[bool] = None


class TranscriptSubmitRequest(BaseModel):
    # transcript defaults to "" (not required) so the same endpoint also
    # accepts an honest "checked, no captions available" result from the
    # home fetcher, not just successful transcripts -- see
    # service.submit_video_transcript's docstring.
    transcript: str = ""
    transcript_note: Optional[str] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/review-queue")
def get_review_queue(
    status: str = Query("pending"),
    product_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    conn = service.get_db_connection()
    try:
        items = service.list_review_queue(conn, status=status, product_id=product_id, limit=limit, offset=offset)
        pending_count = service.get_pending_review_count(conn) if status == "pending" else None
        return {"items": items, "pending_count": pending_count}
    finally:
        conn.close()


@app.get("/review-queue/{review_id}")
def get_review_item(review_id: str):
    conn = service.get_db_connection()
    try:
        item = service.get_review_item(conn, review_id)
        if item is None:
            raise HTTPException(status_code=404, detail="review_queue item not found")
        return item
    finally:
        conn.close()


@app.post("/review-queue/{review_id}/approve")
def approve_review_item(review_id: str, body: ApproveRequest):
    conn = service.get_db_connection()
    try:
        return service.approve_review_item(conn, review_id, body.resolved_by)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        # Covers both "already resolved" and "unrecognized field_name" --
        # the row is left as-is in the DB either way (see
        # service.approve_review_item / build_update_plan), this just
        # reports why it couldn't be applied.
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        conn.close()


@app.post("/review-queue/{review_id}/reject")
def reject_review_item(review_id: str, body: RejectRequest):
    conn = service.get_db_connection()
    try:
        return service.reject_review_item(conn, review_id, body.resolved_by, body.reason)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        conn.close()


@app.get("/brands")
def get_brands():
    # Backs the Products (and Cores) tab's brand filter dropdown -- see
    # service.list_brands' docstring. No query params: this is a short,
    # unpaginated lookup list, not a searchable/filterable collection.
    conn = service.get_db_connection()
    try:
        return {"items": service.list_brands(conn)}
    finally:
        conn.close()


@app.get("/products")
def get_products(
    published: Optional[bool] = Query(None),
    brand_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    needs_video_summary_refresh: Optional[bool] = Query(None),
    has_approved_video_summaries: Optional[bool] = Query(None),
    missing_core: Optional[bool] = Query(None),
    missing_coverstock: Optional[bool] = Query(None),
    source_platform: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    sort: Optional[str] = Query(None, description="'popularity', 'newest', 'oldest', 'name_asc', or 'name_desc' (see service.list_products'/_SORT_ORDER_BY's docstring); omitted/anything else keeps the default recently-updated order"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    conn = service.get_db_connection()
    try:
        return {"items": service.list_products(
            conn, published=published, brand_id=brand_id, search=search,
            needs_video_summary_refresh=needs_video_summary_refresh,
            has_approved_video_summaries=has_approved_video_summaries,
            missing_core=missing_core, missing_coverstock=missing_coverstock,
            source_platform=source_platform, status=status, sort=sort,
            limit=limit, offset=offset,
        )}
    finally:
        conn.close()


@app.get("/products/{product_id}")
def get_product(product_id: str):
    conn = service.get_db_connection()
    try:
        product = service.get_product(conn, product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="product not found")
        return product
    finally:
        conn.close()


@app.patch("/products/{product_id}/published")
def set_product_published(product_id: str, body: PublishRequest):
    conn = service.get_db_connection()
    try:
        return service.set_product_published(conn, product_id, body.published)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


@app.post("/products/{product_id}/rescrape")
def rescrape_product(product_id: str):
    # On-demand rescrape trigger for the cores backfill (migration 007,
    # scripts/backfill_core_ids.py) -- see service.queue_rescrape's
    # docstring. No request body: this republishes the product's own
    # {url, brand_id} onto whichever scrape queue its source_platform
    # maps to. Returns 200 with queued=False (not a 4xx/5xx) when the
    # platform isn't supported yet -- that's an expected, non-error
    # outcome a batch caller logs and moves past, not a request error.
    conn = service.get_db_connection()
    try:
        return service.queue_rescrape(conn, product_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


@app.post("/products/{product_id}/discover-videos")
def discover_videos(product_id: str):
    # On-demand equivalent of a direct `aws lambda invoke
    # bowling-scraper-video-discovery --payload '{"product_ids": [...]}'`
    # -- see service.queue_video_discovery's docstring. No request body:
    # this always scopes to exactly this one product_id. Returns 200 with
    # queued=False (not a 4xx/5xx) when VIDEO_DISCOVERY_FUNCTION_NAME isn't
    # configured on this deployment -- an expected, non-error outcome,
    # same convention as POST /products/{id}/rescrape above.
    conn = service.get_db_connection()
    try:
        return service.queue_video_discovery(conn, product_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


@app.post("/admin/refresh-video-stats")
def refresh_video_stats(limit: Optional[int] = Query(None, gt=0)):
    # On-demand equivalent of a direct `aws lambda invoke
    # bowling-scraper-video-discovery --payload '{"refresh_stats": true}'`
    # -- see service.queue_video_stats_refresh's docstring. Catalog-wide
    # by design (no path param): unlike discover-videos above, there's no
    # single product this scopes to -- VideoDiscoveryFunction itself picks
    # which product_videos rows are most overdue for a refresh. Optional
    # ?limit= caps how many rows get re-pulled in this one invocation;
    # omitted, VideoDiscoveryFunction falls back to its own
    # DEFAULT_REFRESH_STATS_LIMIT. No conn/LookupError handling needed --
    # unlike discover-videos, there's no product_id to validate.
    return service.queue_video_stats_refresh(limit)


@app.patch("/products/{product_id}/images/{image_id}")
def update_product_image(product_id: str, image_id: str, body: ImageUpdateRequest):
    # Per-image visibility/thumbnail toggles (migration 010) -- see
    # service.update_product_image's docstring, in particular why
    # is_thumbnail=True is handled as an atomic "unset every other image
    # on this product, then set this one" operation rather than a bare
    # column write. Distinct from PATCH /products/{id}/published above --
    # that flag controls whether the PRODUCT is visible to the consumer
    # site at all; this one controls whether one specific IMAGE is shown
    # once the product itself is visible.
    conn = service.get_db_connection()
    try:
        return service.update_product_image(conn, product_id, image_id, body.is_visible, body.is_thumbnail)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


@app.patch("/products/{product_id}/plotter-position")
def set_plotter_position(product_id: str, body: PlotterPositionRequest):
    # Writes the AUTHORITATIVE oil/motion chart position (migration 011,
    # digitized from Brunswick's own published Ball Motion Comparison
    # Chart) -- see service.set_plotter_position's docstring. Built for
    # scripts/backfill_plotter_chart_positions.py's one-time matching
    # pass, but works for a manual correction too. 422 (not 404) on an
    # out-of-range value -- psycopg2 surfaces migration 011's own CHECK
    # constraint violation as a real database error, distinct from the
    # 404 a genuinely missing product_id gets.
    conn = service.get_db_connection()
    try:
        return service.set_plotter_position(conn, product_id, body.oil_rating, body.motion_rating, body.source)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        # psycopg2's CheckViolation for an out-of-range rating -- caught
        # broadly (not a specific psycopg2 import) since app.py otherwise
        # has no direct psycopg2 dependency, same pattern as this file's
        # other routes that only ever expect LookupError/ValueError from
        # service.py and treat anything else as a genuine 500 -- except
        # here an out-of-range rating is a real, expected caller mistake
        # (migration 011's CHECK constraint doing its job), worth a 422
        # rather than a bare 500.
        if "check constraint" in str(e).lower() or "products_oil_rating_range" in str(e) or "products_motion_rating_range" in str(e):
            raise HTTPException(status_code=422, detail="oil_rating must be 1-16 and motion_rating must be 1-18")
        raise
    finally:
        conn.close()


@app.post("/products/{product_id}/images/reorder")
def reorder_product_images(product_id: str, body: ImageReorderRequest):
    # Whole-list reorder, not incremental swap endpoints -- see
    # service.reorder_product_images' docstring for why (sidesteps two
    # concurrent partial-swap calls racing each other). No 404 case: an id
    # in image_ids that doesn't belong to product_id is silently ignored
    # rather than erroring, same reasoning as that function's docstring.
    conn = service.get_db_connection()
    try:
        return service.reorder_product_images(conn, product_id, body.image_ids)
    finally:
        conn.close()


@app.post("/admin/backfill-last-video-discovery-at")
def backfill_last_video_discovery_at():
    # One-off correction for migration 005's unbackfilled column -- see
    # service.backfill_last_video_discovery_at's docstring for the full
    # story (a real ~57-product count mismatch this project found live).
    # No request body, no path param: this is catalog-wide by design, not
    # per-product like rescrape/refresh-video-summary above -- there's no
    # per-product decision to make here, just "fill in every row that's
    # missing it". Safe to call more than once (idempotent, see docstring).
    conn = service.get_db_connection()
    try:
        return service.backfill_last_video_discovery_at(conn)
    finally:
        conn.close()


@app.post("/admin/dedupe-price-sources")
def dedupe_price_sources():
    # One-off cleanup for a real duplication bug -- Al: "there are
    # duplicates now, the ones before having the baseurl and now the ones
    # that have it... same record just has different link." See
    # service.dedupe_product_price_sources' docstring for the root cause
    # (a price_sites row's base_url getting filled in after some
    # candidates were already discovered without it) and merge logic.
    # No request body, no path param: catalog-wide by design, same shape
    # as backfill-last-video-discovery-at below. Idempotent -- safe to
    # call again if it's ever needed after another run of discovery.
    conn = service.get_db_connection()
    try:
        return service.dedupe_product_price_sources(conn)
    finally:
        conn.close()


@app.post("/admin/backfill-estimated-plotter-positions")
def backfill_estimated_plotter_positions():
    # One-time (idempotent, safe to re-run) catalog-wide backfill for
    # every product with no plotter position yet -- see service.backfill_
    # estimated_plotter_positions' docstring. Al's own ask after the
    # estimate was being recomputed live on every plotter API call:
    # "back fill the values once in the DB". No request body, no path
    # param: catalog-wide by design, same shape as backfill-last-video-
    # discovery-at above. Run this once after migration 012 deploys, then
    # each scraper's own estimate-on-scrape hook keeps new products
    # covered from here on.
    conn = service.get_db_connection()
    try:
        return service.backfill_estimated_plotter_positions(conn)
    finally:
        conn.close()


@app.post("/admin/backfill-netsuite-status")
def backfill_netsuite_status():
    # One-off correction for the MOTIV status bug -- see
    # service.backfill_netsuite_status's docstring and src/netsuite_
    # product_scraper/app.py's module docstring "REAL INCIDENT" section
    # for the full root-cause writeup (confirmed live: 202/202 MOTIV
    # products showed status='current' despite discovered_urls correctly
    # holding 374 'retired' entries). No request body, no path param:
    # catalog-wide by design, same shape as backfill-last-video-discovery-at
    # above. Safe to call more than once (idempotent, see docstring).
    conn = service.get_db_connection()
    try:
        return service.backfill_netsuite_status(conn)
    finally:
        conn.close()


@app.get("/cores")
def get_cores(
    brand_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    # The "other direction" view of core_name/core_type -- see
    # service.list_cores' docstring. One row per core (not per product),
    # with a product_count so a many-products-to-one-core case (this
    # table's whole reason for existing, per migration 007) is visible at
    # a glance instead of only inferable by noticing several Products-tab
    # rows share a core name.
    conn = service.get_db_connection()
    try:
        return {"items": service.list_cores(conn, brand_id=brand_id, search=search, limit=limit, offset=offset)}
    finally:
        conn.close()


@app.get("/cores/{core_id}")
def get_core(core_id: str):
    conn = service.get_db_connection()
    try:
        core = service.get_core(conn, core_id)
        if core is None:
            raise HTTPException(status_code=404, detail="core not found")
        return core
    finally:
        conn.close()


@app.get("/coverstocks")
def get_coverstocks(
    brand_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    # Same "other direction" view as GET /cores, one migration later
    # (008) -- see service.list_coverstocks' docstring. One row per
    # coverstock (not per product), with a product_count so a
    # many-products-to-one-coverstock case is visible at a glance instead
    # of only inferable by noticing several Products-tab rows share a
    # coverstock name.
    conn = service.get_db_connection()
    try:
        return {"items": service.list_coverstocks(conn, brand_id=brand_id, search=search, limit=limit, offset=offset)}
    finally:
        conn.close()


@app.get("/coverstocks/{coverstock_id}")
def get_coverstock(coverstock_id: str):
    conn = service.get_db_connection()
    try:
        coverstock = service.get_coverstock(conn, coverstock_id)
        if coverstock is None:
            raise HTTPException(status_code=404, detail="coverstock not found")
        return coverstock
    finally:
        conn.close()


@app.post("/products/{product_id}/refresh-video-summary")
def refresh_video_summary(product_id: str):
    # On-demand counterpart to video_summarizer's automatic rollup
    # regeneration (see service.refresh_video_reviews_rollup's docstring)
    # -- for backfilling products whose videos were summarized before this
    # endpoint existed, or re-running after a manual reassign/delete
    # changed which videos count as approved. No request body: this
    # regenerates from whatever's currently approved+summarized, nothing
    # to configure per call.
    conn = service.get_db_connection()
    try:
        return service.refresh_video_reviews_rollup(conn, product_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


# --- Video candidates (YouTube content enrichment) ---
# Same approve/reject shape as /review-queue above; see service.py's
# "Video candidates" section for why this is its own table/workflow rather
# than reusing review_queue's endpoints.

@app.get("/video-candidates")
def get_video_candidates(
    status: str = Query("pending"),
    product_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    # status="all" -- new sentinel for the product detail view's Videos
    # section (see admin-site's loadProductDetailInto and service.
    # list_video_candidates' docstring): shows a product's candidates
    # across every status at once rather than one at a time, which is
    # what actually surfaces a bad approve/auto-approve (e.g. Al's Combat
    # Solid report -- videos approved for it that are really for the
    # Combat/Combat Hybrid siblings). Every other caller of this route is
    # unaffected -- "pending" (the existing default) and any other literal
    # status value still filter exactly as before.
    query_status = None if status == "all" else status
    conn = service.get_db_connection()
    try:
        items = service.list_video_candidates(conn, status=query_status, product_id=product_id, limit=limit, offset=offset)
        pending_count = service.get_pending_video_count(conn) if status == "pending" else None
        return {"items": items, "pending_count": pending_count}
    finally:
        conn.close()


@app.get("/video-candidates/{video_id}")
def get_video_candidate(video_id: str):
    conn = service.get_db_connection()
    try:
        item = service.get_video_candidate(conn, video_id)
        if item is None:
            raise HTTPException(status_code=404, detail="product_videos item not found")
        return item
    finally:
        conn.close()


@app.post("/video-candidates/{video_id}/approve")
def approve_video_candidate(video_id: str, body: ApproveRequest):
    conn = service.get_db_connection()
    try:
        return service.approve_video_candidate(conn, video_id, body.resolved_by)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        conn.close()


@app.post("/video-candidates/{video_id}/reject")
def reject_video_candidate(video_id: str, body: RejectRequest):
    conn = service.get_db_connection()
    try:
        return service.reject_video_candidate(conn, video_id, body.resolved_by, body.reason)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        conn.close()


@app.post("/video-candidates/{video_id}/restore")
def restore_video_candidate(video_id: str):
    # Undo for a mistaken approve/reject -- Al: "it appears if i
    # accidentally reject a video i can not undo that action". No request
    # body (same as DELETE below) -- see service.restore_video_candidate's
    # docstring for why there's no resolved_by to attribute here.
    conn = service.get_db_connection()
    try:
        return service.restore_video_candidate(conn, video_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        conn.close()


@app.post("/video-candidates/{video_id}/reassign")
def reassign_video_candidate(video_id: str, body: ReassignRequest):
    # Correction tool for score_match's known false-positive shape (see
    # service.reassign_video_candidate's docstring) -- e.g. a video for
    # "Storm Absolute Power" that landed on the "Storm Absolute" product.
    # Copies (or merges) the content onto the target product and leaves a
    # rejected tombstone at the origin so it can't resurface there on the
    # next rescan.
    conn = service.get_db_connection()
    try:
        return service.reassign_video_candidate(conn, video_id, body.product_id, body.resolved_by)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        conn.close()


@app.delete("/video-candidates/{video_id}")
def delete_video_candidate(video_id: str):
    # Hard delete, distinct from /reject -- see
    # service.delete_video_candidate's docstring. Mainly the cleanup step
    # for the duplicate a reassign can surface (two products each with
    # their own row for the same YouTube video).
    conn = service.get_db_connection()
    try:
        return service.delete_video_candidate(conn, video_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


@app.post("/video-candidates/{video_id}/transcript")
def submit_video_transcript(video_id: str, body: TranscriptSubmitRequest):
    # Entry point for scripts/home_transcript_fetcher.py -- a transcript
    # fetched from outside AWS entirely (a residential connection) gets
    # published into the exact same VideoTranscriptResultQueue
    # video_transcript_fetcher uses, so video_summarizer treats it
    # identically either way. See service.submit_video_transcript's
    # docstring for why this exists: YouTube's caption-fetch behavior was
    # confirmed identical (blocked) from both a VPC and non-VPC Lambda this
    # session, but works from a residential IP.
    conn = service.get_db_connection()
    try:
        return service.submit_video_transcript(conn, video_id, body.transcript, body.transcript_note)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        conn.close()


@app.get("/price-sites")
def list_price_sites():
    conn = service.get_db_connection()
    try:
        return {"items": service.list_price_sites(conn)}
    finally:
        conn.close()


@app.post("/price-sites")
def create_price_site(body: PriceSiteCreateRequest):
    conn = service.get_db_connection()
    try:
        return service.create_price_site(
            conn, body.name, body.search_url_template, body.result_link_selector,
            body.default_css_selector, body.notes,
            fetch_method=body.fetch_method, api_provider=body.api_provider, base_url=body.base_url,
        )
    finally:
        conn.close()


@app.patch("/price-sites/{site_id}")
def update_price_site(site_id: str, body: PriceSiteUpdateRequest):
    conn = service.get_db_connection()
    try:
        return service.update_price_site(
            conn, site_id, name=body.name, search_url_template=body.search_url_template,
            result_link_selector=body.result_link_selector, default_css_selector=body.default_css_selector,
            notes=body.notes, is_active=body.is_active,
            fetch_method=body.fetch_method, api_provider=body.api_provider, base_url=body.base_url,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


@app.delete("/price-sites/{site_id}")
def delete_price_site(site_id: str):
    conn = service.get_db_connection()
    try:
        return service.delete_price_site(conn, site_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


# --- Price sources (auto-discovered retailer URLs) ---
# Same approve/reject/restore shape as /video-candidates above -- see
# service.py's "Price tracking" section header comment for why: Al's
# final choice was to mirror that exact review workflow rather than
# auto-track a discovered match immediately.

@app.get("/price-sources")
def get_price_sources(
    status: str = Query("pending"),
    product_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    # status="all" sentinel, same as GET /video-candidates above.
    query_status = None if status == "all" else status
    conn = service.get_db_connection()
    try:
        items = service.list_price_sources(conn, status=query_status, product_id=product_id, limit=limit, offset=offset)
        pending_count = service.get_pending_price_source_count(conn) if status == "pending" else None
        return {"items": items, "pending_count": pending_count}
    finally:
        conn.close()


@app.post("/price-sources/{source_id}/approve")
def approve_price_source(source_id: str, body: ApproveRequest):
    conn = service.get_db_connection()
    try:
        return service.approve_price_source(conn, source_id, body.resolved_by)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        conn.close()


@app.post("/price-sources/{source_id}/reject")
def reject_price_source(source_id: str, body: RejectRequest):
    conn = service.get_db_connection()
    try:
        return service.reject_price_source(conn, source_id, body.resolved_by, body.reason)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        conn.close()


@app.post("/price-sources/{source_id}/restore")
def restore_price_source(source_id: str):
    # Undo for a mistaken approve/reject, built in from the start (unlike
    # product_videos, where this only got added after Al hit the gap
    # live) -- see service.restore_price_source's docstring.
    conn = service.get_db_connection()
    try:
        return service.restore_price_source(conn, source_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        conn.close()


@app.get("/products/{product_id}/price-sources")
def list_product_price_sources(product_id: str, status: str = Query("all")):
    # status="all" (the default here, unlike GET /price-sources above) --
    # the product detail view wants to see pending/approved/rejected
    # together by default, same reasoning as GET /video-candidates'
    # product-scoped case (see service.list_product_price_sources'
    # docstring).
    query_status = None if status == "all" else status
    conn = service.get_db_connection()
    try:
        return {"items": service.list_product_price_sources(conn, product_id, status=query_status)}
    finally:
        conn.close()


@app.post("/products/{product_id}/price-sources")
def create_product_price_source(product_id: str, body: ProductPriceSourceCreateRequest):
    # Manual-override path -- see service.create_product_price_source's
    # docstring. Immediately approved (source='manual'), not a candidate
    # to review.
    conn = service.get_db_connection()
    try:
        return service.create_product_price_source(
            conn, product_id, body.price_site_id, body.product_url, body.css_selector, body.resolved_by,
            external_product_id=body.external_product_id,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


@app.patch("/price-sources/{source_id}")
def update_product_price_source(source_id: str, body: ProductPriceSourceUpdateRequest):
    conn = service.get_db_connection()
    try:
        return service.update_product_price_source(
            conn, source_id, product_url=body.product_url, css_selector=body.css_selector,
            is_active=body.is_active,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


@app.delete("/price-sources/{source_id}")
def delete_product_price_source(source_id: str):
    conn = service.get_db_connection()
    try:
        return service.delete_product_price_source(conn, source_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


@app.get("/products/{product_id}/price-history")
def get_price_history(product_id: str, days: int = Query(90, gt=0)):
    # Read side for charting -- see service.get_price_history's docstring.
    # Returns every APPROVED source (for a legend) plus the raw history
    # rows, across all of them at once, so the admin UI can draw one line
    # per source without N separate calls.
    conn = service.get_db_connection()
    try:
        return service.get_price_history(conn, product_id, days=days)
    finally:
        conn.close()


@app.get("/products/{product_id}/sku-stock-history")
def get_sku_stock_history(product_id: str, days: int = Query(90, gt=0)):
    # Read side for per-SKU quantity charting (017_price_tracking_sku_
    # stock.sql) -- see service.get_sku_stock_history's docstring. Al:
    # "for the instock i was refering to actual number of each sku
    # instock." Returns this product's own SKUs (for a legend) plus the
    # raw quantity readings, across all of them at once, same shape as
    # price-history immediately above.
    conn = service.get_db_connection()
    try:
        return service.get_sku_stock_history(conn, product_id, days=days)
    finally:
        conn.close()


@app.post("/products/{product_id}/discover-price-sources")
def discover_price_sources(product_id: str):
    # On-demand "search for price sources" trigger, mirroring discover-
    # videos above -- see service.queue_price_discovery's docstring. No
    # request body: always scopes to exactly this one product_id.
    conn = service.get_db_connection()
    try:
        return service.queue_price_discovery(conn, product_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


@app.post("/admin/discover-all-price-sources")
def discover_all_price_sources(limit: Optional[int] = Query(None, gt=0)):
    # Catalog-wide equivalent of discover-price-sources above -- same
    # shape as /admin/refresh-video-stats. See
    # service.queue_price_discovery_batch's docstring.
    return service.queue_price_discovery_batch(limit)


@app.post("/products/{product_id}/check-price")
def check_price(product_id: str):
    # On-demand trigger for this product's approved price sources -- same
    # fire-and-forget shape as discover-videos above. See
    # service.queue_price_check's docstring.
    conn = service.get_db_connection()
    try:
        return service.queue_price_check(conn, product_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


@app.post("/admin/check-all-prices")
def check_all_prices(limit: Optional[int] = Query(None, gt=0)):
    # Catalog-wide equivalent of check_price above -- same shape as
    # /admin/refresh-video-stats. See service.queue_price_check_batch's
    # docstring.
    return service.queue_price_check_batch(limit)


handler = Mangum(app)
