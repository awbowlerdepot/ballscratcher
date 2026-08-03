"""
YouTube content-enrichment discovery: searches YouTube for candidate review/
reaction videos per product and stores them in product_videos as 'pending'
for an admin to approve or reject via the admin API (see src/admin_api/
service.py's list_video_candidates/approve_video_candidate/
reject_video_candidate, and 004_product_videos.sql's module comment for why
this is a dedicated table rather than reusing review_queue).

Manual/direct invoke only, same convention as this project's other discovery
functions being invoked by hand rather than on a schedule (see
DEPLOY_RUNBOOK.md) -- there's no SQS trigger here. The invoking event
decides scope:
    {"product_ids": ["<uuid>", ...]}   -- specific products
    {"brand_id": "<uuid>"}             -- all published, current products for one brand
    {}                                  -- all published, current products (capped, see below)
This is deliberately NOT "the whole catalog, always" -- the user explicitly
chose a subset-first approach over an immediate full-catalog job, and a
per-invocation scope argument is how every other discovery function in this
project already supports "just run it on what I tell you to" (e.g.
netsuite_url_discovery's BRAND_ID env var, commercebuild's per-brand loop).

HARD QUOTA CONSTRAINT (real, not a guess -- documented in Google's own API
console, same discipline as this project's other real, disclosed
constraints like the AWS account's 10-execution Lambda concurrency
ceiling): YouTube Data API v3's search.list costs 100 quota units per call
against a default project quota of 10,000 units/day. That's a hard ceiling
of ~100 search calls/day per Google Cloud project, and this function makes
exactly one search call per product. MAX_SEARCHES_PER_INVOCATION defaults
to 90 (not 100) to leave headroom for manual re-runs/retries on the same
day without silently blowing the daily quota. If this needs to cover more
than ~90 products/day, that requires either a quota increase request in the
Google Cloud console or spreading invocations across multiple days -- not
something fixable in this code.

Match confidence is a simple two-tier heuristic (see score_match), not a
real relevance score -- YouTube's search API already ranks by its own
relevance signal, but a video titled just "bowling tips" for a query like
"Storm Absolute bowling ball review" would still come back as a top result
sometimes. Rather than guess at a numeric threshold, every candidate is
still stored (never silently dropped), but tagged 'low' confidence when the
brand+product name aren't both recognizable in the title -- this is exactly
what the admin approval step is for.
"""
import json
import logging
import os
import re

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_MAX_RESULTS_PER_PRODUCT = 5
DEFAULT_MAX_SEARCHES_PER_INVOCATION = 90
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

# Generic words stripped when building the "significant tokens" set used by
# score_match -- every product name contains these, so matching on them
# would make almost any bowling video "high confidence".
_STOPWORDS = {"bowling", "ball", "the", "and", "a", "an", "of", "-", "/"}
_WORD_RE = re.compile(r"[a-z0-9]+")


def significant_tokens(name: str) -> set:
    """Lowercased alphanumeric tokens from a product/brand name, minus
    generic bowling-catalog stopwords. Used by score_match to decide if a
    video title plausibly refers to this specific product, not just to
    bowling balls in general."""
    if not name:
        return set()
    return {tok for tok in _WORD_RE.findall(name.lower()) if tok not in _STOPWORDS}


def score_match(title: str, brand_name: str, product_name: str) -> str:
    """Returns 'high' if the video title contains the brand name AND at
    least one significant token from the product name; 'low' otherwise.
    Deliberately permissive (any one product-name token, not all of them)
    since colorway suffixes like "Emerald/Black Hybrid" are often dropped
    from review video titles even when the video is a clear match for the
    base ball name."""
    if not title:
        return "low"
    title_lower = title.lower()

    brand_tokens = significant_tokens(brand_name)
    brand_hit = bool(brand_tokens) and any(tok in title_lower for tok in brand_tokens)

    product_tokens = significant_tokens(product_name)
    product_hit = bool(product_tokens) and any(tok in title_lower for tok in product_tokens)

    return "high" if (brand_hit and product_hit) else "low"


def build_search_query(brand_name: str, product_name: str) -> str:
    return f"{brand_name} {product_name} bowling ball review"


def search_youtube(api_key: str, query: str, max_results: int = DEFAULT_MAX_RESULTS_PER_PRODUCT) -> list:
    """One search.list call (100 quota units -- see module docstring).
    Returns a list of {youtube_video_id, title, channel_title, published_at,
    thumbnail_url} dicts. Kept separate from the DB/looping logic so tests
    can feed a canned response shape without a network call or a real key."""
    import requests

    resp = requests.get(
        YOUTUBE_SEARCH_URL,
        params={
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": max_results,
            "key": api_key,
            "safeSearch": "none",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return parse_search_response(resp.json())


def parse_search_response(data: dict) -> list:
    videos = []
    for item in data.get("items", []):
        video_id = item.get("id", {}).get("videoId")
        if not video_id:
            continue
        snippet = item.get("snippet", {})
        thumbnails = snippet.get("thumbnails", {})
        thumbnail_url = (thumbnails.get("high") or thumbnails.get("default") or {}).get("url")
        videos.append({
            "youtube_video_id": video_id,
            "title": snippet.get("title"),
            "channel_title": snippet.get("channelTitle"),
            "published_at": snippet.get("publishedAt"),
            "thumbnail_url": thumbnail_url,
        })
    return videos


def fetch_products_to_search(conn, job: dict, max_products: int) -> list:
    """Resolves the job's scope (see module docstring) into a list of
    {id, name, brand_name} dicts, capped at max_products. Only published,
    'current' products are considered by default -- retired balls are lower
    priority for review-video enrichment and can be added to product_ids
    explicitly if ever wanted."""
    query = """
        select p.id, p.name, b.name as brand_name
        from products p
        join brands b on b.id = p.brand_id
    """
    params = []
    conditions = []

    product_ids = job.get("product_ids")
    brand_id = job.get("brand_id")

    if product_ids:
        # Real bug found via this deploy's first live smoke test:
        # psycopg2 adapts a plain Python list to an untyped Postgres array
        # literal, which Postgres infers as text[] -- products.id is uuid,
        # and Postgres won't implicitly cast text[] to uuid[] for `= any()`
        # ("operator does not exist: uuid = text"). The explicit ::uuid[]
        # cast on the parameter (not the column) fixes it.
        conditions.append("p.id = any(%s::uuid[])")
        params.append(list(product_ids))
    else:
        conditions.append("p.published = true")
        conditions.append("p.status = 'current'")
        if brand_id:
            conditions.append("p.brand_id = %s")
            params.append(brand_id)

    if conditions:
        query += " where " + " and ".join(conditions)
    query += " order by p.updated_at desc limit %s"
    params.append(max_products)

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def insert_candidates(conn, product_id: str, query: str, videos: list) -> int:
    """Inserts one product_videos row per video, tagged 'pending'.
    ON CONFLICT DO NOTHING makes this idempotent against re-running the same
    product (unique(product_id, youtube_video_id) from 004_product_videos.sql)
    -- a video already stored (in any status) is left untouched rather than
    reset back to pending."""
    inserted = 0
    with conn.cursor() as cur:
        for video in videos:
            confidence = video["match_confidence"]
            cur.execute(
                """
                insert into product_videos
                    (product_id, youtube_video_id, title, channel_title,
                     published_at, thumbnail_url, match_query, match_confidence)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (product_id, youtube_video_id) do nothing
                """,
                (
                    product_id, video["youtube_video_id"], video["title"],
                    video["channel_title"], video["published_at"],
                    video["thumbnail_url"], query, confidence,
                ),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted


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


def get_youtube_api_key() -> str:
    """Reads the YouTube Data API v3 key from Secrets Manager
    (YOUTUBE_API_KEY_SECRET_ARN), mirroring how the DB credentials and the
    BigCommerce/admin-token secrets are fetched elsewhere in this project.
    The key itself has to come from the user (a Google Cloud console API
    key), same "credential this session can't fulfill itself" caveat as
    every other third-party secret in this project."""
    import boto3

    secret_arn = os.environ["YOUTUBE_API_KEY_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    secret_value = client.get_secret_value(SecretId=secret_arn)["SecretString"]
    try:
        return json.loads(secret_value)["api_key"]
    except (ValueError, KeyError):
        # Allow a plain-string secret too, not just a {"api_key": "..."} JSON blob.
        return secret_value


def handler(event, context):
    max_products = int(os.environ.get("MAX_SEARCHES_PER_INVOCATION", DEFAULT_MAX_SEARCHES_PER_INVOCATION))
    max_results = int(os.environ.get("MAX_RESULTS_PER_PRODUCT", DEFAULT_MAX_RESULTS_PER_PRODUCT))

    api_key = get_youtube_api_key()

    conn = get_db_connection()
    try:
        products = fetch_products_to_search(conn, event or {}, max_products)
        logger.info("Searching YouTube for %d product(s) (cap=%d)", len(products), max_products)

        total_candidates = 0
        errors = []
        for product in products:
            query = build_search_query(product["brand_name"], product["name"])
            try:
                videos = search_youtube(api_key, query, max_results)
            except Exception:
                logger.exception("YouTube search failed for product_id=%s query=%r", product["id"], query)
                errors.append(product["id"])
                continue

            for video in videos:
                video["match_confidence"] = score_match(video["title"], product["brand_name"], product["name"])

            inserted = insert_candidates(conn, product["id"], query, videos)
            total_candidates += inserted
            logger.info(
                "product_id=%s query=%r -> %d results, %d new candidates",
                product["id"], query, len(videos), inserted,
            )
    finally:
        conn.close()

    return {
        "statusCode": 200,
        "body": json.dumps({
            "products_searched": len(products),
            "new_candidates": total_candidates,
            "search_errors": len(errors),
        }),
    }
