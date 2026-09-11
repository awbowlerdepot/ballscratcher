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
    # No try/finally conn.close() here (or on any route below) -- see
    # service.get_db_connection's own docstring. This connection is
    # reused across warm Lambda invocations; closing it after every
    # request would defeat that entirely.
    conn = service.get_db_connection()
    return {"items": service.list_brands(conn)}


@app.get("/products")
def get_products(
    status: str = Query("current"),
    brand_id: Optional[str] = Query(None),
    core_id: Optional[str] = Query(None),
    coverstock_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    sort: Optional[str] = Query(None, description="'popularity', 'newest', 'oldest', 'name_asc', or 'name_desc' (see service.list_products'/_SORT_ORDER_BY's docstring); omitted/anything else keeps the default recently-updated order"),
    limit: int = Query(24, le=100),
    offset: int = Query(0, ge=0),
):
    conn = service.get_db_connection()
    return {"items": service.list_products(
        conn, status=status, brand_id=brand_id, core_id=core_id,
        coverstock_id=coverstock_id, search=search, sort=sort,
        limit=limit, offset=offset,
    )}


@app.get("/products/plotter")
def get_products_plotter(
    status: str = Query("current"),
    ids: Optional[str] = Query(None, description="Comma-separated product ids -- overrides status, backs the plotter page's Compare tab"),
):
    # Same literal-path-before-{product_id}-param ordering reasoning as
    # /products/compare below.
    conn = service.get_db_connection()
    id_list = [i.strip() for i in ids.split(",") if i.strip()] if ids else None
    return {"items": service.list_plotter_positions(conn, status=status, ids=id_list)}


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
    return {"items": service.get_products_compare(conn, product_ids)}


@app.get("/articles")
def get_articles(
    brand_id: Optional[str] = Query(None),
    coverstock_id: Optional[str] = Query(None),
    category_id: Optional[str] = Query(None, description="Filter to one Learn-site category (migration 031), e.g. 'Bowling Balls' -- see service.list_categories/GET /categories for the id to pass"),
    search: Optional[str] = Query(None),
    sort: Optional[str] = Query(None, description="'newest', 'oldest', 'title_asc', or 'title_desc' (see service._ARTICLE_SORT_ORDER_BY's docstring); omitted/anything else keeps the default most-recently-approved-first order"),
    limit: int = Query(24, le=100),
    offset: int = Query(0, ge=0),
):
    # Backs the Learn section's browse/index page (learn.bowlerdepot.com)
    # -- see service.list_articles' own docstring. Declared before
    # /products/{product_id} isn't actually necessary here (this is its
    # own top-level "/articles" path, not "/products/..."), but grouped
    # right after /products/plotter and /products/compare above so every
    # literal-path route in this file stays visually together.
    conn = service.get_db_connection()
    return {"items": service.list_articles(
        conn, brand_id=brand_id, coverstock_id=coverstock_id, category_id=category_id,
        search=search, sort=sort, limit=limit, offset=offset,
    )}


@app.get("/categories")
def get_categories():
    # Learn-site content taxonomy (migration 031) -- see
    # service.list_categories' own docstring. Backs the Learn nav's
    # "Bowling Balls" / "Ball Review" labels and any future category
    # switcher, read from data rather than hardcoded into the frontend.
    conn = service.get_db_connection()
    return {"items": service.list_categories(conn)}


@app.get("/products/{product_id}")
def get_product(product_id: str):
    conn = service.get_db_connection()
    product = service.get_product(conn, product_id)
    if product is None:
        # Deliberately identical 404 whether the id doesn't exist or
        # exists but isn't published -- see service.get_product's
        # docstring for why that distinction must not leak here.
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@app.get("/products/{product_id}/similar")
def get_similar_products(product_id: str, limit: int = Query(5, le=20)):
    conn = service.get_db_connection()
    return {"items": service.list_similar_products(conn, product_id, limit=limit)}


@app.get("/products/{product_id}/article")
def get_product_article(product_id: str):
    # Ball-review article (022_product_articles.sql) -- see
    # service.get_product_article's docstring. 404 only for a
    # nonexistent/unpublished product_id (same non-distinction as
    # GET /products/{id} above); an existing published product with no
    # APPROVED article yet still returns 200 with article: None, same
    # always-200 contract as the bowlerdepot video-summary embed route
    # below.
    conn = service.get_db_connection()
    result = service.get_product_article(conn, product_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return result


@app.get("/bowlerdepot/products/{bigcommerce_product_id}/video-summary")
def get_bowlerdepot_video_summary(bigcommerce_product_id: str):
    # Backs the embed script running on live bowlerdepot.com product
    # pages (see service.get_video_summary_by_bigcommerce_product_id's
    # docstring) -- always 200, never 404, since "no match/no summary
    # yet" is the normal case for most of BowlerDepot's catalog, not an
    # error the script needs to special-case.
    conn = service.get_db_connection()
    return service.get_video_summary_by_bigcommerce_product_id(conn, bigcommerce_product_id)


@app.get("/bowlerdepot/products/{bigcommerce_product_id}/article-hero")
def get_bowlerdepot_article_hero(bigcommerce_product_id: str):
    # Backs the sibling embed script adding a "Review" tab to live
    # bowlerdepot.com product pages (see service.get_article_hero_by_
    # bigcommerce_product_id's docstring) -- same always-200, never-404
    # contract as video-summary above.
    conn = service.get_db_connection()
    return service.get_article_hero_by_bigcommerce_product_id(conn, bigcommerce_product_id)


handler = Mangum(app)
