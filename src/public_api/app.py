"""
FastAPI public read-only API, deployed to Lambda behind API Gateway via
Mangum -- the data source for the consumer-facing site (see service.py's
module docstring for the full "why a separate function from admin_api"
reasoning: no auth at all here, by design, and every query is hard-scoped
to published = true).

Thin routing layer only, same split as admin_api/app.py and for the same
reason: fastapi/pydantic aren't installable in this sandbox (pip's proxy
returns 403), so this file's routes are logic-verified via service.py's
own tests (tests/test_public_api_service.py) but not actually executed
here. Review this file with a bit more scrutiny than code that was
actually run.
"""
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from mangum import Mangum

import service

app = FastAPI(title="Bowling Ball Public API")


@app.get("/health")
def health():
    # No DB round-trip on purpose -- this just confirms the Lambda itself
    # is up and fastapi/mangum imported cleanly, same shallow check
    # admin_api's own /health-adjacent auth probe relies on (see
    # DEPLOY_RUNBOOK.md 6a). A real data check happens on every other
    # route anyway.
    return {"status": "ok"}


@app.get("/brands")
def get_brands():
    conn = service.get_db_connection()
    try:
        return {"items": service.list_brands(conn)}
    finally:
        conn.close()


@app.get("/products")
def get_products(
    status: str = Query("current"),
    brand_id: Optional[str] = Query(None),
    core_id: Optional[str] = Query(None),
    coverstock_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    sort: Optional[str] = Query(None, description="'popularity' for the view-count-decay ranking (see service.list_products' docstring); omitted/anything else keeps the default recently-updated order"),
    limit: int = Query(24, le=100),
    offset: int = Query(0, ge=0),
):
    conn = service.get_db_connection()
    try:
        return {"items": service.list_products(
            conn, status=status, brand_id=brand_id, core_id=core_id,
            coverstock_id=coverstock_id, search=search, sort=sort,
            limit=limit, offset=offset,
        )}
    finally:
        conn.close()


@app.get("/products/plotter")
def get_products_plotter(
    status: str = Query("current"),
    ids: Optional[str] = Query(None, description="Comma-separated product ids -- overrides status, backs the plotter page's Compare tab"),
):
    # Same literal-path-before-{product_id}-param ordering reasoning as
    # /products/compare below.
    conn = service.get_db_connection()
    try:
        id_list = [i.strip() for i in ids.split(",") if i.strip()] if ids else None
        return {"items": service.list_plotter_positions(conn, status=status, ids=id_list)}
    finally:
        conn.close()


@app.get("/products/compare")
def get_products_compare(ids: str = Query(..., description="Comma-separated product ids")):
    # A real, deliberate ordering dependency: this route is declared
    # BEFORE /products/{product_id} below so FastAPI's path-matching
    # tries the literal "/products/compare" first -- otherwise "compare"
    # would be swallowed as a product_id path param and 404 as a
    # not-found/unpublished product instead of running this route at all.
    product_ids = [i.strip() for i in ids.split(",") if i.strip()]
    if not product_ids:
        raise HTTPException(status_code=422, detail="ids must contain at least one product id")
    conn = service.get_db_connection()
    try:
        return {"items": service.get_products_compare(conn, product_ids)}
    finally:
        conn.close()


@app.get("/products/{product_id}")
def get_product(product_id: str):
    conn = service.get_db_connection()
    try:
        product = service.get_product(conn, product_id)
        if product is None:
            # Deliberately identical 404 whether the id doesn't exist or
            # exists but isn't published -- see service.get_product's
            # docstring for why that distinction must not leak here.
            raise HTTPException(status_code=404, detail="Product not found")
        return product
    finally:
        conn.close()


@app.get("/products/{product_id}/similar")
def get_similar_products(product_id: str, limit: int = Query(5, le=20)):
    conn = service.get_db_connection()
    try:
        return {"items": service.list_similar_products(conn, product_id, limit=limit)}
    finally:
        conn.close()


handler = Mangum(app)
