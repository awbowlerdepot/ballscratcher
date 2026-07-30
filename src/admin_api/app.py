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


@app.get("/products")
def get_products(
    published: Optional[bool] = Query(None),
    brand_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    conn = service.get_db_connection()
    try:
        return {"items": service.list_products(conn, published=published, brand_id=brand_id, search=search, limit=limit, offset=offset)}
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


handler = Mangum(app)
