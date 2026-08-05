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
    {"product_ids": ["<uuid>", ...]}   -- specific products, any status/published value
    {"brand_id": "<uuid>"}             -- all 'current' (non-retired) products for one brand
    {}                                  -- all 'current' (non-retired) products (capped, see below)
This is deliberately NOT "the whole catalog, always" -- the user explicitly
chose a subset-first approach over an immediate full-catalog job, and a
per-invocation scope argument is how every other discovery function in this
project already supports "just run it on what I tell you to" (e.g.
netsuite_url_discovery's BRAND_ID env var, commercebuild's per-brand loop).

The default/brand_id scopes deliberately do NOT require `published = true`
(they only used to, until a real catalog check found why that mattered:
142 'current' products, but only 1 with `published = true` -- almost the
entire catalog would never get discovered under that filter). Video
discovery is meant to run ahead of publishing, per an explicit product
decision: candidates should already be found (and ideally approved) by the
time a product actually goes live, not discovered from scratch afterward.
`status = 'current'` (excluding retired balls) is still applied -- that
part of the scoping was never in question, only `published`.

ROTATION (real bug found in production, fixed via 005_products_last_
video_discovery_at.sql): the default/brand_id scopes used to order by
`p.updated_at desc` -- but nothing in this pipeline ever touches that
column, so every {} invocation re-selected the exact same top-N products,
forever. The documented "run {} once a day to cover the whole catalog
under the ~90/day quota" pattern (DEPLOY_RUNBOOK.md 6i) never actually
progressed past the first day's batch. Fixed by ordering on
`last_video_discovery_at asc nulls first` instead: never-searched products
always sort first, and mark_product_searched (called from handler, success
path only -- see its docstring for why errors don't count) records when a
product was actually covered. Once the whole catalog has been searched at
least once, this naturally cycles back to the least-recently-searched
products, which is also correct behavior long-term (new review videos get
posted well after a ball's release).

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

REAL INCIDENT, first 90-product run against the whole never-searched
backlog (Track/Ebonite's brand-new catalogs plus everything else that had
never been discovered): 813 new candidates, but 38 of the 90 searches
failed. CloudWatch showed every failure was the SAME cause -- a 429 from
search.list with reason "rateLimitExceeded" against quota metric "Search
Queries per minute", NOT the daily 10,000-unit ceiling described above
(90 calls is only 9,000 units, comfortably under that). This is a
DIFFERENT, shorter-window limit: YouTube throttles how fast you can call
search.list, not just how many calls/day -- and search_youtube() was
firing all 90 requests back-to-back with no pacing or retry at all, so
once the per-minute cap was hit partway through, every remaining call in
that burst got rejected. Same failure shape as the Shopify 429s and the
Lambda-concurrency 503s found earlier in this project (an unthrottled
loop outrunning someone else's rate limit) -- fixed the same way, retry-
with-backoff via get_youtube_requests_session() below, reusing the exact
RETRY_TOTAL/RETRY_BACKOFF_FACTOR/RETRY_STATUS_FORCELIST naming convention
scripts/backfill_core_ids.py already established for this. No data was
actually lost from the incident itself -- mark_product_searched only
runs on the success path, so all 38 failed products stayed
last_video_discovery_at=NULL and were still eligible for the very next
{} invocation -- but without this fix, a re-run risks hitting the same
wall again partway through. VideoDiscoveryFunction's Timeout in
template.yaml was also raised (150s -> 280s) to give the retry backoff
room to actually wait out a per-minute window without the Lambda itself
timing out mid-recovery.
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

# Retried status codes: 429 is the real, confirmed cause here (YouTube's
# per-minute search.list rate limit -- see module docstring's incident
# writeup), 500/502/503/504 included as the same "probably transient"
# bucket every other retry-enabled request in this project uses.
# backoff_factor=1 with urllib3's default formula
# (backoff_factor * 2^(retry_number-1)) waits 1s/2s/4s/8s/16s between the
# 5 attempts -- and, importantly, urllib3's Retry honors a Retry-After
# response header when present (respect_retry_after_header defaults to
# True), so a request that DOES get a real reset hint from Google waits
# exactly that long instead of guessing.
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)
RETRY_TOTAL = 5
RETRY_BACKOFF_FACTOR = 1

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


def get_youtube_requests_session():
    """Builds a requests.Session with urllib3 Retry mounted on https --
    see RETRY_STATUS_FORCELIST/RETRY_TOTAL/RETRY_BACKOFF_FACTOR's comment
    and the module docstring's incident writeup for why this exists (a
    real, confirmed per-minute YouTube rate limit, not a guess). A fresh
    session per call rather than one module-level singleton keeps this
    easy to monkeypatch/replace in tests without cross-test state -- same
    reasoning as scripts/backfill_core_ids.py's identically-named
    function. handler() below builds ONE session and reuses it across
    every product in a given invocation (connection pooling), rather than
    one per search_youtube call, but a fresh default is still provided
    here for any direct/manual call that doesn't pass one."""
    import requests
    from urllib3.util.retry import Retry

    session = requests.Session()
    retry = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUS_FORCELIST,
        allowed_methods=("GET",),
        raise_on_status=False,  # let the resp.ok check below report the final failure, not urllib3's own exception shape
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


def search_youtube(api_key: str, query: str, max_results: int = DEFAULT_MAX_RESULTS_PER_PRODUCT,
                    session=None) -> list:
    """One search.list call (100 quota units -- see module docstring).
    Returns a list of {youtube_video_id, title, channel_title, published_at,
    thumbnail_url} dicts. Kept separate from the DB/looping logic so tests
    can feed a canned response shape without a network call or a real key.
    session defaults to a fresh retry-enabled one (see
    get_youtube_requests_session) but is overridable so tests can inject a
    fake transport instead of hitting the network, and so handler() can
    pass down one shared session for the whole invocation."""
    import requests

    session = session if session is not None else get_youtube_requests_session()

    resp = session.get(
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
    if not resp.ok:
        # Real gap found via this deploy's first live smoke test: a bare
        # resp.raise_for_status() only surfaces "403 Forbidden" in
        # CloudWatch, not WHY -- Google's error body (error.errors[0].reason,
        # e.g. "accessNotConfigured"/"keyInvalid"/"quotaExceeded") is the
        # actually-actionable part and was getting swallowed. Truncated to
        # keep a pathological response from bloating the log line; the key
        # itself is a request param, not part of the response body, so it
        # doesn't get echoed back into this message.
        raise requests.exceptions.HTTPError(
            f"{resp.status_code} error from YouTube search.list: {resp.text[:500]}",
            response=resp,
        )
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
    {id, name, brand_name} dicts, capped at max_products. 'current' (non-
    retired) products only, by default -- retired balls are lower priority
    for review-video enrichment and can be added to product_ids explicitly
    if ever wanted. Deliberately does NOT require published = true (see
    module docstring's real catalog numbers on why that was dropped) --
    discovery is meant to run ahead of publishing, not after it.

    Default/brand_id scopes order by last_video_discovery_at asc nulls
    first (see module docstring's ROTATION section) -- this is what makes
    repeated {} invocations actually progress through the catalog instead
    of re-selecting the same top-N products every time. product_ids scope
    ignores that column entirely (an explicit list is already a deliberate
    choice, not something to rotate) but still orders by p.id for
    deterministic results when len(product_ids) > max_products."""
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
        conditions.append("p.status = 'current'")
        if brand_id:
            conditions.append("p.brand_id = %s")
            params.append(brand_id)

    if conditions:
        query += " where " + " and ".join(conditions)

    if product_ids:
        query += " order by p.id asc limit %s"
    else:
        query += " order by p.last_video_discovery_at asc nulls first, p.id asc limit %s"
    params.append(max_products)

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def mark_product_searched(conn, product_id: str) -> None:
    """Records that video_discovery actually completed a search.list call
    for this product -- called from handler's success path only (after
    search_youtube returns without raising), never from the except branch.
    A search that errors (transient network issue, or real: quota
    exhaustion -- see the module docstring's HARD QUOTA CONSTRAINT section,
    something this project has genuinely hit) never really searched
    anything; crediting it as 'searched' would push that product to the
    back of the rotation queue past products that were never attempted at
    all, which is exactly backwards. See fetch_products_to_search's
    ordering (last_video_discovery_at asc nulls first) for what this
    column drives."""
    with conn.cursor() as cur:
        cur.execute(
            "update products set last_video_discovery_at = now() where id = %s",
            (product_id,),
        )
    conn.commit()


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
    # One session, reused across every product this invocation -- see
    # get_youtube_requests_session's docstring (connection pooling) and
    # the module docstring's incident writeup (this is also where the
    # actual retry-with-backoff protection against YouTube's per-minute
    # rate limit comes from; search_youtube's own default fallback exists
    # for direct/manual calls that skip handler entirely).
    session = get_youtube_requests_session()

    conn = get_db_connection()
    try:
        products = fetch_products_to_search(conn, event or {}, max_products)
        logger.info("Searching YouTube for %d product(s) (cap=%d)", len(products), max_products)

        total_candidates = 0
        errors = []
        for product in products:
            query = build_search_query(product["brand_name"], product["name"])
            try:
                videos = search_youtube(api_key, query, max_results, session=session)
            except Exception:
                logger.exception("YouTube search failed for product_id=%s query=%r", product["id"], query)
                errors.append(product["id"])
                continue

            for video in videos:
                video["match_confidence"] = score_match(video["title"], product["brand_name"], product["name"])

            inserted = insert_candidates(conn, product["id"], query, videos)
            mark_product_searched(conn, product["id"])
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
