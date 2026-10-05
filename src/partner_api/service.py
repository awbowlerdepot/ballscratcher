"""
Partner API v1 -- data access and mapping (DEPLOY_RUNBOOK.md 6cj).

Al: "we are also working on a separate platform that i want to be a
consumer of the ball data in the project. I feel like that being just an
API integration is doable and not sure if we would want a lightweight
version of security ontop of that just to keep it in house" -- then:
"server-side, read-only, public data for now, it could sync so that isn't
as noisy. One other thing that might be helpful is locking the shape down
so that if we move the learn and consumer sites forward it doesn't break
the integration to other platform".

So this is its own Lambda (not routes on public_api) with its own queries
and its own mapping into models.py -- public_api can change for the Learn/
consumer sites without moving anything here. Read-only, published data
only, same as public_api. Auth is the partner_api_authorizer in front of
it (per-partner keys); nothing in this module checks keys.

The mapping functions (row -> v1 dict) are pure and unit tested; the SQL
is exercised against a scratch Postgres (see the runbook section).
"""
import base64
import json
import math
import os
import uuid
from datetime import datetime, timezone

# Module-level connection cache -- same warm-Lambda reuse as public_api's
# get_db_connection (see that module for the incident behind it).
_cached_conn = None
_cached_secret = None

LEARN_SITE_URL = os.environ.get("LEARN_SITE_URL", "https://learn.bowlerdepot.com").rstrip("/")

DEFAULT_PAGE_SIZE = 200
MAX_PAGE_SIZE = 500
# /v1/changes only returns changes at least this old. A write's
# content_changed_at is its transaction's start time, so a long transaction
# can commit AFTER a poll that already moved past that timestamp; holding
# back the newest minute means a slow commit is never skipped.
CHANGE_LAG_SECONDS = 60

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ZERO_ID = "00000000-0000-0000-0000-000000000000"


def _connect_with_secret(secret):
    import psycopg2

    conn = psycopg2.connect(
        host=secret["host"],
        port=secret.get("port", 5432),
        dbname=secret["dbname"],
        user=secret["username"],
        password=secret["password"],
    )
    conn.autocommit = True
    return conn


def _fetch_secret():
    import boto3

    client = boto3.client("secretsmanager")
    return json.loads(client.get_secret_value(SecretId=os.environ["DB_SECRET_ARN"])["SecretString"])


def get_db_connection():
    """Cached across warm invocations, health-checked with select 1, one
    re-fetch of the secret on an auth failure (credential rotation)."""
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
        _cached_secret = _fetch_secret()
        _cached_conn = _connect_with_secret(_cached_secret)
    return _cached_conn


# --------------------------------------------------------------------
# Cursors -- opaque to the partner (base64url JSON), so their shape can
# change without breaking anyone who just stores and echoes them.
# --------------------------------------------------------------------

class InvalidCursor(ValueError):
    pass


def encode_cursor(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if not isinstance(data, dict):
            raise ValueError
        return data
    except Exception as e:  # noqa: BLE001 -- any malformed input is the same client error
        raise InvalidCursor("cursor is not one this API issued") from e


def _cursor_id(value) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError) as e:
        raise InvalidCursor("cursor is not one this API issued") from e


def clamp_limit(limit) -> int:
    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(MAX_PAGE_SIZE, int(limit)))


# --------------------------------------------------------------------
# Row -> v1 mapping (pure)
# --------------------------------------------------------------------

def _float_or_none(value):
    return float(value) if value is not None else None


def map_weight(row: dict) -> dict:
    return {
        "weight_lbs": int(row["weight_lbs"]),
        "rg": _float_or_none(row.get("rg")),
        "differential": _float_or_none(row.get("differential")),
        "mass_bias": _float_or_none(row.get("mass_bias")),
    }


def map_plotter(row: dict):
    if row.get("oil_rating") is None or row.get("motion_rating") is None:
        return None
    return {
        "oil": float(row["oil_rating"]),
        "motion": float(row["motion_rating"]),
        "source": row.get("oil_motion_source") or "estimated",
    }


def map_ball(row: dict, weights: list) -> dict:
    """One products row (BALL_SELECT columns) + its SKU rows -> v1 Ball."""
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "brand": {"id": str(row["brand_id"]), "name": row["brand_name"]},
        "status": row["status"],
        "release_date": row.get("release_date"),
        "color": row.get("color"),
        "coverstock": {
            "name": row.get("coverstock_name"),
            "material": row.get("coverstock_material"),
            "type": row.get("coverstock_type"),
        },
        "core": {"name": row.get("core_name"), "type": row.get("core_type")},
        "has_particle": bool(row.get("has_particle")),
        "factory_finish": row.get("factory_finish"),
        "finish_category": row.get("finish_category"),
        "weights": [map_weight(w) for w in sorted(weights, key=lambda w: -int(w["weight_lbs"]))],
        "plotter": map_plotter(row),
        "image_url": row.get("image_url"),
        "manufacturer_url": row["url"],
        "learn_article_url": f"{LEARN_SITE_URL}/articles/{row['article_slug']}" if row.get("article_slug") else None,
        "bowlerdepot_url": row.get("bowlerdepot_url"),
        "content_changed_at": row["content_changed_at"],
    }


# --------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------

# Every column map_ball reads. coverstock_name prefers the coverstocks
# table's normalized name, falling back to the product's raw one. Image:
# same rule as public_api (visible thumbnail first, then display_order,
# else the scraped primary_image_url). bowlerdepot_url: the approved,
# active BigCommerce price source (same rule as public_api's
# ecommerce_url). article_slug: the approved Learn article.
BALL_SELECT = """
    select p.id, p.name, p.brand_id, b.name as brand_name, p.status, p.release_date, p.color,
           coalesce(cv.name, p.coverstock_name) as coverstock_name,
           p.coverstock_material, p.coverstock_type,
           c.name as core_name, c.core_type,
           p.has_particle, p.factory_finish, p.finish_category,
           p.oil_rating, p.motion_rating, p.oil_motion_source,
           coalesce(
               (select coalesce(pi.stored_url, pi.source_url) from product_images pi
                 where pi.product_id = p.id and pi.is_visible = true
                 order by pi.is_thumbnail desc, pi.display_order, pi.id limit 1),
               p.primary_image_url
           ) as image_url,
           p.url,
           (select pa.slug from product_articles pa
             where pa.product_id = p.id and pa.status = 'approved' and pa.slug is not null
             order by pa.first_published_at desc nulls last limit 1) as article_slug,
           (select pps.product_url from product_price_sources pps
              join price_sites ps on ps.id = pps.price_site_id
             where pps.product_id = p.id and ps.api_provider = 'bigcommerce'
               and pps.status = 'approved' and pps.is_active = true
             order by pps.last_checked_at desc nulls last, pps.id limit 1) as bowlerdepot_url,
           p.content_changed_at, p.published, p.core_id, p.coverstock_id
    from products p
    join brands b on b.id = p.brand_id
    left join cores c on c.id = p.core_id
    left join coverstocks cv on cv.id = p.coverstock_id
"""

_PUBLISHED_BALL = "p.product_type = 'ball' and p.published = true"


def _rows(cur):
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, r)) for r in cur.fetchall()]


def _weights_for(cur, product_ids: list) -> dict:
    if not product_ids:
        return {}
    cur.execute(
        "select product_id, weight_lbs, rg, differential, mass_bias from product_skus "
        "where product_id = any(%s::uuid[]) order by weight_lbs desc",
        ([str(i) for i in product_ids],),
    )
    out = {}
    for r in _rows(cur):
        out.setdefault(str(r["product_id"]), []).append(r)
    return out


def _balls_from_rows(cur, rows: list) -> list:
    weights = _weights_for(cur, [r["id"] for r in rows])
    return [map_ball(r, weights.get(str(r["id"]), [])) for r in rows]


def list_brands(conn) -> list:
    """Brands with at least one published ball."""
    with conn.cursor() as cur:
        cur.execute(
            f"select b.id, b.name from brands b where exists "
            f"(select 1 from products p where p.brand_id = b.id and {_PUBLISHED_BALL}) order by b.name"
        )
        return [{"id": str(r["id"]), "name": r["name"]} for r in _rows(cur)]


def list_balls(conn, cursor: str = None, limit: int = None) -> dict:
    """Every published ball, current and retired, in id order (stable
    pages for a full load). next_cursor is null on the last page."""
    limit = clamp_limit(limit)
    after_id = _cursor_id(decode_cursor(cursor).get("id")) if cursor else _ZERO_ID
    with conn.cursor() as cur:
        cur.execute(
            BALL_SELECT + f" where {_PUBLISHED_BALL} and p.id > %s::uuid order by p.id limit %s",
            (after_id, limit + 1),
        )
        rows = _rows(cur)
        more = len(rows) > limit
        rows = rows[:limit]
        items = _balls_from_rows(cur, rows)
    return {"items": items, "next_cursor": encode_cursor({"id": str(rows[-1]["id"])}) if more and rows else None}


def get_ball(conn, ball_id: str):
    try:
        ball_id = str(uuid.UUID(ball_id))
    except (ValueError, TypeError, AttributeError):
        return None  # a malformed id is just "not found" to a partner
    with conn.cursor() as cur:
        cur.execute(BALL_SELECT + f" where {_PUBLISHED_BALL} and p.id = %s::uuid", (ball_id,))
        rows = _rows(cur)
        if not rows:
            return None
        return _balls_from_rows(cur, rows)[0]


def parse_since(since: str) -> datetime:
    try:
        dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except Exception as e:  # noqa: BLE001
        raise InvalidCursor("since must be an ISO 8601 timestamp, e.g. 2026-10-05T00:00:00Z") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def list_changes(conn, cursor: str = None, since: str = None, limit: int = None) -> dict:
    """The sync feed. Walks balls in (content_changed_at, id) order from
    the cursor (or `since`, or the beginning), at most `limit` per page,
    stopping CHANGE_LAG_SECONDS short of now.

    A published ball is an 'upsert' carrying the full Ball; an unpublished
    one is a 'remove' (so the partner deletes its copy). On a first sync
    (no cursor, no since) removes are skipped -- there's nothing to delete
    yet -- but the cursor still advances past them.

    next_cursor is always returned: after the last row scanned, or the
    position you came in with if nothing new was ready. Changes to the
    same ball collapse: you only ever get its latest state."""
    limit = clamp_limit(limit)
    if cursor:
        data = decode_cursor(cursor)
        try:
            pos_t = parse_since(data["t"])
            pos_id = _cursor_id(data["id"])
        except (KeyError, InvalidCursor) as e:
            raise InvalidCursor("cursor is not one this API issued") from e
        initial = False
    elif since:
        pos_t, pos_id, initial = parse_since(since), _ZERO_ID, False
    else:
        pos_t, pos_id, initial = _EPOCH, _ZERO_ID, True

    with conn.cursor() as cur:
        cur.execute(
            BALL_SELECT
            + " where p.product_type = 'ball'"
            "   and (p.content_changed_at, p.id) > (%s, %s::uuid)"
            "   and p.content_changed_at < now() - make_interval(secs => %s)"
            " order by p.content_changed_at, p.id limit %s",
            (pos_t, pos_id, CHANGE_LAG_SECONDS, limit + 1),
        )
        rows = _rows(cur)
        has_more = len(rows) > limit
        rows = rows[:limit]
        published = [r for r in rows if r["published"]]
        balls = {b["id"]: b for b in _balls_from_rows(cur, published)}

    items = []
    for r in rows:
        rid = str(r["id"])
        if r["published"]:
            items.append({"id": rid, "change": "upsert", "changed_at": r["content_changed_at"], "ball": balls[rid]})
        elif not initial:
            items.append({"id": rid, "change": "remove", "changed_at": r["content_changed_at"], "ball": None})
    if rows:
        last = rows[-1]
        next_cursor = encode_cursor({"t": last["content_changed_at"].isoformat(), "id": str(last["id"])})
    else:
        next_cursor = encode_cursor({"t": pos_t.isoformat(), "id": pos_id})
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


def list_plotter(conn) -> list:
    """Every current published ball that has a stored position, with the
    same neighbors the Learn plotter shows (compute_plotter_neighbors,
    kept identical to public_api's by tests/test_plotter_neighbors_sync.py).
    Positions come only from stored ratings -- no live estimate here."""
    with conn.cursor() as cur:
        cur.execute(
            BALL_SELECT
            + f" where {_PUBLISHED_BALL} and p.status = 'current' and p.oil_rating is not null and p.motion_rating is not null"
            " order by p.id"
        )
        rows = _rows(cur)
    points = [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "brand_name": r["brand_name"],
            "oil": float(r["oil_rating"]),
            "motion": float(r["motion_rating"]),
            "recommendable": bool(r["bowlerdepot_url"]),
            "core_id": r["core_id"],
            "coverstock_id": r["coverstock_id"],
        }
        for r in rows
    ]
    neighbors = compute_plotter_neighbors(points)
    return [
        {"id": str(r["id"]), "plotter": map_plotter(r), "neighbors": neighbors[str(r["id"])]}
        for r in rows
    ]


# >>> PLOTTER NEIGHBORS v1 >>>
# Copied verbatim into src/partner_api/service.py (Partner API /v1/plotter,
# runbook 6cj); tests/test_plotter_neighbors_sync.py fails if they differ.
# Colorways are one ball for recommendations -- suggesting three Rhinos
# as "twins" is noise. "Same ball" = same brand + core + coverstock (what
# actually makes the reaction; Storm doesn't use a " - " colorway
# separator, e.g. "TROPICAL SURGE TEAL-BLUE"), falling back to the name
# before " - " when core or cover is unknown.
_PLOTTER_COLORWAY_SPLIT = " - "

# Twins: other-brand ball lines within this distance (plotter units).
PLOTTER_TWIN_RADIUS = 1.5
PLOTTER_TWIN_MAX = 3
# Directional steps: "a bit more or less" -- at least STEP_MIN along the
# axis (a 0.2 nudge isn't a different ball), at most STEP_MAX (beyond that
# it's a different category, not "a bit"), and inside a 45-degree cone so
# "more oil" doesn't hand back something mostly more angular.
PLOTTER_STEP_MIN = 0.5
PLOTTER_STEP_MAX = 4.0
PLOTTER_STEP_PER_DIRECTION = 2
# Prefer a small step that stays on-axis: score = along + weight * off-axis.
PLOTTER_STEP_OFF_AXIS_WEIGHT = 1.5
PLOTTER_DIRECTIONS = {
    "more_oil": (1.0, 0.0),
    "less_oil": (-1.0, 0.0),
    "more_angular": (0.0, 1.0),
    "smoother": (0.0, -1.0),
}


def plotter_ball_line(point: dict) -> tuple:
    if point.get("core_id") and point.get("coverstock_id"):
        return (point["brand_name"], "spec", str(point["core_id"]), str(point["coverstock_id"]))
    return (point["brand_name"], "name", (point.get("name") or "").split(_PLOTTER_COLORWAY_SPLIT)[0].strip().lower())


def _plotter_best_per_line(candidates: list) -> list:
    """candidates: (score, point) pairs, best first after sorting. Keeps
    the best-scoring colorway of each (brand, line)."""
    seen, out = set(), []
    for score, point in sorted(candidates, key=lambda c: (c[0], c[1]["name"], c[1]["id"])):
        key = plotter_ball_line(point)
        if key in seen:
            continue
        seen.add(key)
        out.append((score, point))
    return out


def compute_plotter_neighbors(points: list) -> dict:
    """Pure function (no DB): {product_id: {"twins": [...], "more_oil":
    [...], "less_oil": [...], "more_angular": [...], "smoother": [...]}}.
    Every entry is {"id", "distance"}. points: dicts with id, name,
    brand_name, oil, motion, "recommendable" (sold at BowlerDepot), and
    optionally core_id/coverstock_id (see plotter_ball_line). Every ball
    gets neighbors (so a ball BowlerDepot doesn't sell still points to
    ones it does); only recommendable balls are ever suggested, never
    another colorway of the ball itself, and a twin isn't repeated as a
    directional step -- each suggestion is a distinct ball."""
    recommendable = [p for p in points if p.get("recommendable")]
    result = {}
    for target in points:
        own_line = plotter_ball_line(target)
        others = [p for p in recommendable if p["id"] != target["id"] and plotter_ball_line(p) != own_line]

        twin_candidates = []
        for p in others:
            if p["brand_name"] == target["brand_name"]:
                continue  # twins are the cross-brand question
            d = math.hypot(p["oil"] - target["oil"], p["motion"] - target["motion"])
            if d <= PLOTTER_TWIN_RADIUS:
                twin_candidates.append((d, p))
        twins = _plotter_best_per_line(twin_candidates)[:PLOTTER_TWIN_MAX]
        entry = {"twins": [{"id": p["id"], "distance": round(d, 2)} for d, p in twins]}
        twin_lines = {plotter_ball_line(p) for _, p in twins}

        for direction, (ux, uy) in PLOTTER_DIRECTIONS.items():
            step_candidates = []
            for p in others:
                if plotter_ball_line(p) in twin_lines:
                    continue
                dx, dy = p["oil"] - target["oil"], p["motion"] - target["motion"]
                along = dx * ux + dy * uy
                off = abs(dx * uy - dy * ux)
                if PLOTTER_STEP_MIN <= along <= PLOTTER_STEP_MAX and off <= along:
                    step_candidates.append((along + PLOTTER_STEP_OFF_AXIS_WEIGHT * off, p))
            steps = _plotter_best_per_line(step_candidates)[:PLOTTER_STEP_PER_DIRECTION]
            entry[direction] = [
                {"id": p["id"], "distance": round(math.hypot(p["oil"] - target["oil"], p["motion"] - target["motion"]), 2)}
                for _, p in steps
            ]
        result[target["id"]] = entry
    return result

# <<< PLOTTER NEIGHBORS v1 <<<
