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
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse
from mangum import Mangum

import models
import service

# API REFERENCE (runbook 6ee). Al: "we have another entity that wants to use
# it. can we start with some api documentation? the last time i did this we
# used swagger but can we use whatever the modern most used api
# documentation is". The schema stays OpenAPI 3.1 (what "Swagger" became),
# generated from models.py so the docs can't drift from the contract; the
# human guide that lived in docs/partner-api-v1.md is folded into it below.
# It's rendered by Scalar at /docs (public page, Al's pick), with live "Try
# it" (Al's pick): the page is served from the API's own origin
# (api.bowleriq.io/partner/docs), so the browser's calls are same-origin and
# need no CORS; the partner pastes their key into the page and it's sent
# only to this API. /docs and /openapi.json are public routes in
# template.yaml (no key needed to READ the docs); every /v1 route still
# requires a key.
DOCS_DESCRIPTION = """
Read-only bowling ball data for BowlerIQ partner platforms: specs per weight,
cover and core, ball motion plotter positions with suggestions, and links to
BowlerDepot Learn reviews and BowlerDepot product pages.

## Getting started

1. **Get a key.** BowlerIQ issues one key per partner platform. Keep it
   server-side: never ship it in a browser or mobile app.
2. **Send it on every request** as `Authorization: Bearer <your key>`.
   To try calls on this page, click **Authentication** and paste your key;
   it's sent only to `api.bowleriq.io`.
3. **Load everything once**, then **keep in sync** with `GET /v1/changes`
   (see *Syncing* below).

```bash
curl -s -H "Authorization: Bearer $BOWLERIQ_PARTNER_KEY" \\
  "https://api.bowleriq.io/partner/v1/changes?limit=50"
```

## Basics

- **Format:** JSON. Every response includes `"api_version": "1"`.
- **Rate limit:** about 10 requests/second, bursts of 20. Over that you get
  `429`; back off and retry.
- **Paging:** `limit` defaults to 200, maximum 500. Follow `next_cursor`.
- **Ids** are stable UUIDs; use them as your keys. Each colorway is its own ball.

## The v1 promise

v1 only **grows**: we may add optional fields and new endpoints, so ignore
fields you don't recognise. Within v1 we will never rename or remove a field
or endpoint, change a field's type, units or meaning, or make an optional
field required. Any of those ships as `/v2`, and `/v1` keeps running while
you migrate.

## Syncing

```text
cursor = load_saved_cursor()                    # None on the very first run
loop:
    page = GET /v1/changes?cursor=<cursor>      # omit cursor the first time
    for change in page.items:
        if change.change == "upsert": save(change.ball)   # create/replace by id
        if change.change == "remove": delete(change.id)
    cursor = page.next_cursor
    save_cursor(cursor)                         # after applying the page
    if not page.has_more: break
```

- Run it every 15-60 minutes. The **first run** returns every published ball
  as an `upsert`; to start from a point in time pass `?since=2026-10-01T00:00:00Z`.
- **Always store `next_cursor`** and send it next time. Don't use the time of
  your last run as `since`: that can skip changes.
- Re-running from your last saved cursor is safe; upserts and removes can be
  applied more than once.
- A ball appears once per page with its latest state. Changes appear about
  60 seconds after they happen.
- A ball counts as changed when its own fields, any weight's specs, its
  images or its Learn review link change.
- `demand` and `bigcommerce_product_id` are refreshed daily and do **not**
  count as a change (they'd make every ball change every day). Read them
  from `GET /v1/balls`; `demand.computed_at` says how fresh they are.

## Errors

| Status | Meaning |
|---|---|
| `400` | Bad `cursor` or `since`. Restart from your last good cursor. |
| `401` / `403` | Missing or invalid key |
| `404` | Ball not found or not published |
| `429` | Rate limited; retry with backoff |
| `5xx` | Our problem; retry with backoff |
"""

OPENAPI_TAGS = [
    {"name": "Sync", "description": "Keep your copy of the catalog up to date. Start here."},
    {"name": "Balls", "description": "Full loads and single-ball lookups."},
    {"name": "Brands", "description": "Brands with published balls."},
    {"name": "Plotter", "description": "Ball motion plotter positions and suggestions."},
    {
        "name": "Patterns",
        "description": "Oil patterns: the machine program, board totals and a per-board, per-foot oil grid "
        "(`profile`), synced like balls with their own feed. Every pattern is checked against its sheet "
        "and approved by BowlerIQ staff before it appears here.",
    },
]

app = FastAPI(
    title="BowlerIQ Partner API",
    version="1",
    summary="Read-only bowling ball data for BowlerIQ partner platforms.",
    description=DOCS_DESCRIPTION,
    openapi_tags=OPENAPI_TAGS,
    servers=[{"url": "https://api.bowleriq.io/partner", "description": "Production"}],
    # FastAPI's own Swagger UI / ReDoc pages stay off; /docs below serves
    # Scalar instead (see API REFERENCE above).
    docs_url=None,
    redoc_url=None,
)

# Error responses shown on every route in the docs.
_AUTH_ERRORS = {
    401: {"model": models.GatewayError, "description": "Missing key."},
    403: {"model": models.GatewayError, "description": "Invalid key."},
    429: {"model": models.GatewayError, "description": "Rate limited; retry with backoff."},
}
_CURSOR_ERRORS = {**_AUTH_ERRORS, 400: {"model": models.ErrorDetail, "description": "Bad cursor or since."}}


def _openapi():
    """FastAPI's schema plus the Bearer key scheme, so the docs show how to
    authenticate and "Try it" sends the key. Only adds to the schema."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title, version=app.version, summary=app.summary, description=app.description,
        routes=app.routes, tags=app.openapi_tags, servers=app.servers,
    )
    schema.setdefault("components", {})["securitySchemes"] = {
        "PartnerKey": {
            "type": "http",
            "scheme": "bearer",
            "description": "Your BowlerIQ partner key, sent as `Authorization: Bearer <key>`.",
        }
    }
    schema["security"] = [{"PartnerKey": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _openapi

# Scalar, pinned to an exact version (1.72.3, released 2026-09-30) so a
# library release can't change the page under partners.
SCALAR_VERSION = "1.72.3"
DOCS_HTML = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>BowlerIQ Partner API</title>
    <meta name="description" content="API reference for the BowlerIQ Partner API v1." />
  </head>
  <body>
    <div id="app"></div>
    <script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference@{SCALAR_VERSION}/dist/browser/standalone.js"></script>
    <script>
      Scalar.createApiReference('#app', {{
        url: 'openapi.json',
        hideClientButton: true,
        // No Scalar toolbar (Configure/Share/Deploy), "Ask AI" chat or
        // "Generate MCP" button: partners get a plain reference.
        // persistAuth stays at its default (false), so a pasted key isn't
        // kept in the browser between visits.
        showDeveloperTools: 'never',
        agent: {{ disabled: true }},
        mcp: {{ disabled: true }},
        defaultHttpClient: {{ targetKey: 'shell', clientKey: 'curl' }},
        authentication: {{ preferredSecurityScheme: 'PartnerKey' }},
        metaData: {{ title: 'BowlerIQ Partner API' }},
      }})
    </script>
  </body>
</html>
"""


@app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
def docs_page():
    return DOCS_HTML


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


@app.get("/v1/brands", response_model=models.BrandList, summary="Brands with at least one published ball",
         tags=["Brands"], responses=_AUTH_ERRORS)
def get_brands():
    return {"items": service.list_brands(service.get_db_connection())}


@app.get(
    "/v1/balls",
    response_model=models.BallPage,
    summary="All published balls (full load), paged by id",
    description="Every published ball, current and retired. Good for a one-time full load; use /v1/changes to stay in sync.",
    tags=["Balls"],
    responses=_CURSOR_ERRORS,
)
def get_balls(
    cursor: Optional[str] = Query(None, description="next_cursor from the previous page."),
    limit: int = Query(service.DEFAULT_PAGE_SIZE, ge=1, le=service.MAX_PAGE_SIZE),
):
    try:
        return service.list_balls(service.get_db_connection(), cursor=cursor, limit=limit)
    except service.InvalidCursor as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/v1/balls/{ball_id}", response_model=models.BallResponse, summary="One published ball", tags=["Balls"],
         responses={**_AUTH_ERRORS, 404: {"model": models.ErrorDetail, "description": "Not found or not published."}})
def get_ball(ball_id: str):
    ball = service.get_ball(service.get_db_connection(), ball_id)
    if ball is None:
        raise HTTPException(status_code=404, detail="No published ball with that id")
    return {"item": ball}


@app.get(
    "/v1/changes",
    response_model=models.ChangePage,
    summary="Sync feed: balls changed since your last cursor",
    tags=["Sync"],
    responses=_CURSOR_ERRORS,
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
        page = service.list_changes(service.get_db_connection(), cursor=cursor, since=since, limit=limit)
    except service.InvalidCursor as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Diagnostics only (runbook 6dm) -- Al: "i just ran an update on the api
    # in the drillsheet software and it still didn't find them in the change
    # sset". The access log had the path but not HOW a partner tracks its
    # place, which is the usual reason a sync misses changes (?since=<last
    # run time> instead of the stored next_cursor). Never logs the cursor
    # itself, just which mode, plus what came back.
    # The module's own "partner_api" logger (INFO enabled), not the root
    # logger -- the root drops INFO on Lambda, which hid the first version.
    position = cursor and "cursor" or (since and f"since={since}") or "from-start"
    logger.info("changes %s limit=%s -> items=%d has_more=%s", position, limit,
                len(page["items"]), page["has_more"])
    return page


@app.get(
    "/v1/plotter",
    response_model=models.PlotterList,
    summary="Ball motion plotter: positions + similar balls and directional steps for every current ball",
    tags=["Plotter"],
    responses=_AUTH_ERRORS,
)
def get_plotter():
    return {"items": service.list_plotter(service.get_db_connection())}


# --- Oil patterns (runbook 6ej; docs/partner-api-v1-patterns.md) ---------
# /v1/patterns/changes is declared before /v1/patterns/{pattern_id} so
# "changes" isn't read as an id.


@app.get(
    "/v1/patterns",
    response_model=models.PatternPage,
    summary="All published oil patterns (full load), paged by id",
    tags=["Patterns"],
    responses=_CURSOR_ERRORS,
)
def get_patterns(
    cursor: Optional[str] = Query(None, description="next_cursor from the previous page."),
    limit: int = Query(service.DEFAULT_PAGE_SIZE, ge=1, le=service.MAX_PAGE_SIZE),
):
    try:
        return service.list_patterns(service.get_db_connection(), cursor=cursor, limit=limit)
    except service.InvalidCursor as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get(
    "/v1/patterns/changes",
    response_model=models.PatternChangePage,
    summary="Pattern sync feed: patterns changed since your last cursor",
    description=(
        "Same rules as /v1/changes, for patterns: first sync with no cursor (or ?since=), page until "
        "has_more is false, store next_cursor, and next time pass ?cursor=<stored>. 'upsert' = create or "
        "replace your copy; 'remove' = delete it. "
        f"Changes appear about {service.CHANGE_LAG_SECONDS} seconds after they happen."
    ),
    tags=["Patterns"],
    responses=_CURSOR_ERRORS,
)
def get_pattern_changes(
    cursor: Optional[str] = Query(None, description="next_cursor from your previous call."),
    since: Optional[str] = Query(None, description="ISO 8601 time to start from when you have no cursor."),
    limit: int = Query(service.DEFAULT_PAGE_SIZE, ge=1, le=service.MAX_PAGE_SIZE),
):
    try:
        page = service.list_pattern_changes(service.get_db_connection(), cursor=cursor, since=since, limit=limit)
    except service.InvalidCursor as e:
        raise HTTPException(status_code=400, detail=str(e))
    position = cursor and "cursor" or (since and f"since={since}") or "from-start"
    logger.info("pattern changes %s limit=%s -> items=%d has_more=%s", position, limit, len(page["items"]), page["has_more"])
    return page


@app.get(
    "/v1/patterns/{pattern_id}",
    response_model=models.PatternResponse,
    summary="One published oil pattern",
    tags=["Patterns"],
    responses={**_AUTH_ERRORS, 404: {"model": models.ErrorDetail, "description": "Not found or not published."}},
)
def get_pattern(pattern_id: str):
    pattern = service.get_pattern(service.get_db_connection(), pattern_id)
    if pattern is None:
        raise HTTPException(status_code=404, detail="No published pattern with that id")
    return {"item": pattern}


# api_gateway_base_path: on api.bowleriq.io/partner/... the event path can
# still carry the "/partner" mapping prefix; Mangum strips it only when
# present, so the raw execute-api URL (/v1/...) works too.
handler = Mangum(app, api_gateway_base_path="/partner")
