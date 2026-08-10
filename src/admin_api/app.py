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
from typing import Optional

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


class ReassignRequest(BaseModel):
    product_id: str


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
            source_platform=source_platform,
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


@app.post("/video-candidates/{video_id}/reassign")
def reassign_video_candidate(video_id: str, body: ReassignRequest):
    # Correction tool for score_match's known false-positive shape (see
    # service.reassign_video_candidate's docstring) -- e.g. a video for
    # "Storm Absolute Power" that landed on the "Storm Absolute" product.
    # Moves the row, keeps any transcript/summary already fetched.
    conn = service.get_db_connection()
    try:
        return service.reassign_video_candidate(conn, video_id, body.product_id)
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


handler = Mangum(app)
