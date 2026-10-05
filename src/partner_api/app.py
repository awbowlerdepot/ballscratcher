"""
Partner API v1 -- routes (DEPLOY_RUNBOOK.md 6cj). Read-only ball data for
in-house partner platforms, behind partner_api_authorizer (per-partner
bearer keys). Served at https://api.bowleriq.io/partner/v1/... (an API
mapping on the public API's domain; API Gateway strips "/partner", so the
routes here start at /v1).

Every route declares a response_model from models.py -- FastAPI validates
each response against it, so a query change that would alter the shape
fails loudly instead of quietly changing what partners receive. The
committed OpenAPI snapshot (tests/fixtures/partner_api_v1_openapi.json)
locks the shape itself; see models.py for the v1 change rules.
"""
import logging
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from mangum import Mangum

import models
import service

app = FastAPI(
    title="BowlerIQ Partner API",
    version="1",
    # No /docs or /redoc: those pages fetch the schema from a browser,
    # which can't send the partner key. /openapi.json stays (curl it with
    # the key); docs/partner-api-v1.md is the human guide.
    docs_url=None,
    redoc_url=None,
    description=(
        "Read-only bowling ball data for BowlerIQ partner platforms. "
        "Authenticate with `Authorization: Bearer <partner key>`. "
        "v1 only ever grows (new optional fields/endpoints); breaking changes ship as /v2."
    ),
)


logger = logging.getLogger("partner_api")
logger.setLevel(logging.INFO)


@app.middleware("http")
async def log_partner_call(request: Request, call_next):
    """One log line per call naming the partner (from the authorizer's
    context), so usage is attributable per key in CloudWatch."""
    started = time.time()
    response = await call_next(request)
    event = request.scope.get("aws.event") or {}
    partner = ((event.get("requestContext") or {}).get("authorizer") or {}).get("lambda", {}).get("partner")
    logger.info(
        "partner=%s %s %s -> %s in %dms",
        partner or "?", request.method, request.url.path, response.status_code, (time.time() - started) * 1000,
    )
    return response


@app.get("/v1/brands", response_model=models.BrandList, summary="Brands with at least one published ball")
def get_brands():
    return {"items": service.list_brands(service.get_db_connection())}


@app.get("/v1/balls", response_model=models.BallPage, summary="All published balls (full load), paged by id")
def get_balls(
    cursor: Optional[str] = Query(None, description="next_cursor from the previous page."),
    limit: int = Query(service.DEFAULT_PAGE_SIZE, ge=1, le=service.MAX_PAGE_SIZE),
):
    try:
        return service.list_balls(service.get_db_connection(), cursor=cursor, limit=limit)
    except service.InvalidCursor as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/v1/balls/{ball_id}", response_model=models.BallResponse, summary="One published ball")
def get_ball(ball_id: str):
    ball = service.get_ball(service.get_db_connection(), ball_id)
    if ball is None:
        raise HTTPException(status_code=404, detail="No published ball with that id")
    return {"item": ball}


@app.get(
    "/v1/changes",
    response_model=models.ChangePage,
    summary="Sync feed: balls changed since your last cursor",
    description=(
        "First sync: call with no cursor (or ?since=ISO time) and page until has_more is false. "
        "Store next_cursor; next time call ?cursor=<stored> to get only newer changes. "
        "'upsert' = create/replace your copy; 'remove' = delete it. "
        f"Changes appear about {service.CHANGE_LAG_SECONDS} seconds after they happen."
    ),
)
def get_changes(
    cursor: Optional[str] = Query(None, description="next_cursor from your previous call."),
    since: Optional[str] = Query(None, description="ISO 8601 time to start from when you have no cursor."),
    limit: int = Query(service.DEFAULT_PAGE_SIZE, ge=1, le=service.MAX_PAGE_SIZE),
):
    try:
        return service.list_changes(service.get_db_connection(), cursor=cursor, since=since, limit=limit)
    except service.InvalidCursor as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get(
    "/v1/plotter",
    response_model=models.PlotterList,
    summary="Ball motion plotter: positions + similar balls and directional steps for every current ball",
)
def get_plotter():
    return {"items": service.list_plotter(service.get_db_connection())}


# api_gateway_base_path: on api.bowleriq.io/partner/... the event path can
# still carry the "/partner" mapping prefix; Mangum strips it only when
# present, so the raw execute-api URL (/v1/...) works too.
handler = Mangum(app, api_gateway_base_path="/partner")
