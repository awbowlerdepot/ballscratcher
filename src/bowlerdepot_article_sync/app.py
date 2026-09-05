"""
Pushes approved, admin-flagged ball-review articles
(022_product_articles.sql, src/product_article_generator) onto
bowlerdepot.com as native BigCommerce Blog posts -- the sync job spec'd
out in DEPLOY_RUNBOOK.md section 6u, now built.

Follow-up chain that led here: Al asked how to get these AI-generated
articles onto bowlerdepot.com as a knowledge-base section; two
architectures were discussed (push into BigCommerce as native content vs.
a decoupled microsite reading public_api live) and Al chose to add an
admin-controlled per-article toggle first (028_product_articles_
bigcommerce_sync.sql: product_articles.sync_to_bigcommerce/
bigcommerce_post_id/bowlerdepot_synced_at) and have the sync job spec'd
separately. Al then flipped the flag on a real article via curl, got back
a clean 200, and reported "i don't see it in bigcommerce" -- correct,
since at that point the flag was the only thing built. This module is
the actual push.

Same "push into BigCommerce's own native feature rather than reinventing
one" reasoning bowlerdepot_video_sync (Al's earlier ask, "pull in the
video section into the video section of the bigcommerce product page")
already established, applied to a different BigCommerce API:

**Confirmed live against BigCommerce's own docs this session** (not
recalled from training data): unlike bowlerdepot_video_sync's v3 Catalog
Product Videos endpoint, Blog Posts are a v2 API --
POST /stores/{store_hash}/v2/blog/posts, required fields title/body,
response is the created object DIRECTLY (no "data" wrapper -- this is
the one real shape difference from bowlerdepot_video_sync's
push_video_to_bigcommerce, which unwraps resp.json()["data"]; get this
wrong and mark_article_synced would store the string "id" instead of the
real integer). OAuth scope needed is store_v2_content (Content: modify)
-- CONFIRMED a DIFFERENT scope than bowlerdepot_video_sync's Products-
modify (store_v2_products) need. Whatever token sits behind
BIGCOMMERCE_SECRET_ARN today (built for price_checker's read-only
lookups, then upgraded for bowlerdepot_video_sync's Products-modify
need) almost certainly does NOT have Content-modify yet -- Al needs to
check/add it in BigCommerce's own API Accounts settings before this
job's first real run, same "unverifiable from this sandbox, real
prerequisite Al must handle himself" situation as that function's own
docstring already flags for its own scope. A missing scope surfaces as a
plain 403 on push_article_to_bigcommerce, logged per-article by
_process_one_article -- nothing upstream (the flag toggle, the needs-
sync query) can detect this ahead of time.

**Product-page link (open question in the original spec, now resolved --
Al's choice)**: bowlerdepot_products has no stored bowlerdepot.com
product URL (migration 001 only has bigcommerce_product_id/
bigcommerce_sku/match_status), so fetch_bigcommerce_product_url calls
BigCommerce's OWN Catalog v3 API (GET /v3/catalog/products/{id}) at sync
time to read that product's current custom_url.url, rather than adding a
new stored column. This is deliberately best-effort: a failure here
(network error, deleted BigCommerce product, wrong scope on this
particular call -- Catalog reads use store_v2_products_read_only, a
DIFFERENT scope again from the Blog write) logs a warning and the post
still goes out, just without a "View this ball" link, rather than
blocking the whole article on a link lookup. custom_url.url comes back
as a site-relative path (e.g. "/brunswick-combat-solid/"); used as-is in
the anchor href since the blog post and the product live on the same
bowlerdepot.com domain.

**Needs-sync query** (list_articles_needing_sync) mirrors
bowlerdepot_video_sync's list_videos_needing_sync shape exactly:
product_articles.status = 'approved' AND sync_to_bigcommerce = true AND
bowlerdepot_synced_at is null, joined through bowlerdepot_products scoped
to match_status = 'matched' only -- an 'ambiguous'/'unmatched' row is a
known-unreliable match by that module's own design (see the real Storm
iQ Tour / iQ Tour AI suffix-collision incident, 6h.1); this job must
never post an article under the wrong BigCommerce product.

**Body construction** (build_article_body_html) assembles the article's
structured fields (hook, performance_summary, who_should_buy/skip,
pros/cons, buying_tips, verdict, faq) into one HTML string. Images
(action_shot_image_url/product_shot_image_url) are inlined as plain
<img src="..."> tags pointing straight at their existing public S3 URLs
-- these are already served directly to the consumer site via
public_api, so there's no new hosting problem to solve.
thumbnail_path is deliberately NOT set -- per BigCommerce's own docs it
requires a separate WebDAV upload to /product_images/ first, real added
complexity for a cosmetic thumbnail that isn't required to publish the
post at all (see the original spec's own reasoning).

**Idempotency**: bigcommerce_post_id/bowlerdepot_synced_at (migration
028) mirror product_videos.bowlerdepot_video_id/bowlerdepot_synced_at
exactly -- a re-run only pushes a not-yet-synced row. Turning
sync_to_bigcommerce off after a successful sync does NOT delete/
unpublish anything (migration 028's own header comment) -- this module
has no delete path at all, by design; that's still explicitly out of
scope. Regenerating an article (which resets status back to 'pending',
per 022's no-versioning design) does not retroactively touch an
already-published BigCommerce post either -- updating/deleting a
previously-synced post on regenerate is still future scope, same as the
original spec flagged.

**Two invocation shapes, one handler** -- same "scheduled batch, plus an
on-demand single-item path checked first" convention product_article_
generator.app.handler already uses for `event.get("product_id")`:
- No `article_id` in the event (the hourly Schedule trigger, see
  template.yaml): processes every currently-due article in one
  invocation, same "run once, do everything outstanding" shape as
  BowlerdepotVideoSyncFunction.
- `{"article_id": "..."}` (admin_api's queue_article_sync, the Articles
  tab's "Sync now" button -- Al wanted to test this immediately rather
  than wait up to an hour): scopes list_articles_needing_sync to that one
  row. Still requires the row to actually meet every needs-sync
  condition (approved + flag on + not yet synced + matched) -- this is
  "sync THIS one now instead of waiting for the schedule," not "force-
  sync regardless of state." An article that doesn't qualify (e.g. the
  flag is off, or it's already synced) simply yields zero pushes, same
  as it would on the next scheduled run -- not an error.
"""
import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BIGCOMMERCE_API_BASE = "https://api.bigcommerce.com"
DEFAULT_FETCH_TIMEOUT_SECONDS = 30


def get_bigcommerce_credentials():
    """Identical to price_checker/bowlerdepot_reconciliation/
    bowlerdepot_video_sync's own get_bigcommerce_credentials -- duplicated,
    not imported/shared, same "each Lambda owns its own deploy package"
    convention used throughout this codebase.

    Unlike bowlerdepot_video_sync (Products-modify, store_v2_products),
    THIS module's push_article_to_bigcommerce needs the token to carry the
    Content-modify scope (store_v2_content) -- confirmed via BigCommerce's
    own docs this session. Whether the configured secret's token actually
    has that scope isn't something this function (or anything in this
    sandbox) can verify -- Al needs to confirm/upgrade it in BigCommerce's
    own API Accounts settings before this function's pushes will succeed;
    a 403 from push_article_to_bigcommerce is the symptom if it doesn't."""
    import boto3

    secret_arn = os.environ["BIGCOMMERCE_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    secret = json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])
    return secret["store_hash"], secret["auth_token"]


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


def list_articles_needing_sync(conn, article_id: str = None) -> list:
    """Every approved, admin-flagged article for a product BigCommerce
    reconciliation confidently matched (match_status='matched') that
    hasn't been pushed to BigCommerce yet (bowlerdepot_synced_at is
    null) -- see this module's own docstring for the full reasoning,
    same shape as bowlerdepot_video_sync.list_videos_needing_sync.

    article_id, when given (the on-demand "Sync now" path), scopes the
    query to exactly that one row instead of every outstanding one --
    still subject to every other condition, so a not-yet-approved or
    not-yet-flagged article simply yields an empty list rather than
    being force-synced."""
    query = """
        select pa.id as article_id, pa.product_id, bp.bigcommerce_product_id,
               pa.title, pa.hook, pa.performance_summary, pa.who_should_buy,
               pa.who_should_skip, pa.pros, pa.cons, pa.buying_tips, pa.verdict,
               pa.faq, pa.action_shot_image_url, pa.product_shot_image_url,
               b.name as brand_name
        from product_articles pa
        join bowlerdepot_products bp on bp.product_id = pa.product_id
        join products p on p.id = pa.product_id
        join brands b on b.id = p.brand_id
        where pa.status = 'approved'
          and pa.sync_to_bigcommerce = true
          and pa.bowlerdepot_synced_at is null
          and bp.match_status = 'matched'
    """
    params = []
    if article_id:
        query += " and pa.id = %s"
        params.append(article_id)
    query += " order by pa.id"

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def fetch_bigcommerce_product_url(session, store_hash: str, auth_token: str, bigcommerce_product_id: str):
    """Best-effort lookup of the product's own storefront URL via
    BigCommerce's Catalog v3 API (GET /v3/catalog/products/{id}), so the
    blog post can link back to the ball it's about -- see this module's
    own docstring for why this is a live API call rather than a stored
    column. Returns None (never raises) on ANY failure -- a bad lookup
    should degrade to "post without a link," not block the whole article
    from going out. Response shape: {"data": {"custom_url": {"url": ...},
    ...}} -- note this IS the v3 "data"-wrapped shape (unlike the v2 Blog
    Posts response below), since Catalog is a v3 API."""
    try:
        resp = session.get(
            f"{BIGCOMMERCE_API_BASE}/stores/{store_hash}/v3/catalog/products/{bigcommerce_product_id}",
            headers={"Accept": "application/json", "X-Auth-Token": auth_token},
            params={"include_fields": "custom_url"},
            timeout=DEFAULT_FETCH_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        custom_url = resp.json().get("data", {}).get("custom_url") or {}
        return custom_url.get("url") or None
    except Exception:
        logger.warning("Could not fetch BigCommerce product URL for bigcommerce_product_id=%s -- "
                        "posting without a product link", bigcommerce_product_id, exc_info=True)
        return None


def build_article_body_html(article: dict, product_url: str = None) -> str:
    """Pure function -- assembles product_articles' structured fields into
    one HTML string for the Blog Post's `body` field. See this module's
    own docstring for why images are inlined by URL rather than uploaded
    via thumbnail_path."""
    def esc(s):
        return (
            (s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    def list_block(label, items):
        if not items:
            return ""
        lis = "".join(f"<li>{esc(item)}</li>" for item in items)
        return f"<p><strong>{label}:</strong></p><ul>{lis}</ul>"

    parts = []
    if article.get("action_shot_image_url"):
        parts.append(f'<img src="{esc(article["action_shot_image_url"])}" alt="{esc(article.get("title"))}" />')
    if article.get("product_shot_image_url"):
        parts.append(f'<img src="{esc(article["product_shot_image_url"])}" alt="{esc(article.get("title"))}" />')
    if article.get("hook"):
        parts.append(f"<p><em>{esc(article['hook'])}</em></p>")
    if article.get("performance_summary"):
        parts.append(f"<p><strong>Performance summary:</strong></p><p>{esc(article['performance_summary'])}</p>")
    parts.append(list_block("Who should buy this", article.get("who_should_buy")))
    parts.append(list_block("Who should skip this", article.get("who_should_skip")))
    parts.append(list_block("Pros", article.get("pros")))
    parts.append(list_block("Cons", article.get("cons")))
    if article.get("buying_tips"):
        parts.append(f"<p><strong>Buying tips:</strong></p><p>{esc(article['buying_tips'])}</p>")
    if article.get("verdict"):
        parts.append(f"<p><strong>Verdict:</strong></p><p>{esc(article['verdict'])}</p>")
    for qa in (article.get("faq") or []):
        parts.append(f"<p><strong>{esc(qa.get('question'))}</strong></p><p>{esc(qa.get('answer'))}</p>")
    if product_url:
        parts.append(f'<p><a href="{esc(product_url)}">View this ball on BowlerDepot</a></p>')
    return "".join(p for p in parts if p)


def build_bigcommerce_blog_post_payload(article: dict, product_url: str = None) -> dict:
    """Maps one product_articles row to BigCommerce's documented Create
    Blog Post request body (confirmed live via their own docs this
    session): required title/body, plus is_published=true (posts default
    to draft otherwise) and tags=[brand_name] when known. author is set
    to "BowlerDepot" -- there's no per-article author concept in this
    schema, and leaving it blank is also a valid choice BigCommerce
    supports, but a named author reads better on a real blog."""
    return {
        "title": article.get("title") or "",
        "body": build_article_body_html(article, product_url),
        "is_published": True,
        "author": "BowlerDepot",
        "tags": [article["brand_name"]] if article.get("brand_name") else [],
    }


def push_article_to_bigcommerce(session, store_hash: str, auth_token: str, payload: dict) -> dict:
    """POSTs one blog post, returns the created post object. IMPORTANT:
    unlike bowlerdepot_video_sync's push_video_to_bigcommerce (a v3 API,
    response wrapped in a "data" key), Blog Posts are a v2 API and the
    response IS the created object directly -- resp.json() itself, no
    unwrapping. Confirmed against BigCommerce's own docs this session,
    not assumed from the v3 shape. Raises on a non-2xx response
    (resp.raise_for_status()) -- caller (_process_one_article) is what
    catches that per-article, so one bad push doesn't abort the whole
    batch."""
    resp = session.post(
        f"{BIGCOMMERCE_API_BASE}/stores/{store_hash}/v2/blog/posts",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Auth-Token": auth_token,
        },
        json=payload,
        timeout=DEFAULT_FETCH_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp.json()


def mark_article_synced(conn, article_id: str, bigcommerce_post_id) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update product_articles
            set bigcommerce_post_id = %s, bowlerdepot_synced_at = now()
            where id = %s
            """,
            (str(bigcommerce_post_id), article_id),
        )
    conn.commit()


def _process_one_article(session, conn, store_hash: str, auth_token: str, article: dict) -> dict:
    """Pushes one article and records the result -- exceptions are caught
    HERE (not left to propagate) so handler's loop can keep going through
    the rest of a scheduled batch after one article's push fails (e.g. a
    403 from a not-yet-upgraded OAuth scope, or a stale
    bigcommerce_product_id)."""
    try:
        product_url = fetch_bigcommerce_product_url(
            session, store_hash, auth_token, article["bigcommerce_product_id"],
        )
        payload = build_bigcommerce_blog_post_payload(article, product_url)
        created = push_article_to_bigcommerce(session, store_hash, auth_token, payload)
        mark_article_synced(conn, article["article_id"], created["id"])
        return {"article_id": str(article["article_id"]), "success": True, "bigcommerce_post_id": created["id"]}
    except Exception as exc:
        logger.exception(
            "Failed to push article %s (product %s, bigcommerce_product_id %s) to BigCommerce",
            article.get("article_id"), article.get("product_id"), article.get("bigcommerce_product_id"),
        )
        return {"article_id": str(article.get("article_id")), "success": False, "error": str(exc)}


def handler(event, context):
    """See this module's own docstring for the two invocation shapes
    (scheduled batch vs. on-demand single article_id)."""
    import requests

    article_id = (event or {}).get("article_id")
    conn = get_db_connection()
    try:
        articles = list_articles_needing_sync(conn, article_id=article_id)
        if not articles:
            logger.info("No articles need syncing to BigCommerce (article_id=%s)", article_id)
            return {"statusCode": 200, "body": json.dumps({"pushed": 0, "failed": 0, "results": []})}

        store_hash, auth_token = get_bigcommerce_credentials()
        session = requests.Session()

        results = [_process_one_article(session, conn, store_hash, auth_token, article) for article in articles]

        pushed = sum(1 for r in results if r["success"])
        failed = len(results) - pushed
        logger.info("BigCommerce article sync: %d pushed, %d failed", pushed, failed)
        return {"statusCode": 200, "body": json.dumps({"pushed": pushed, "failed": failed, "results": results})}
    finally:
        conn.close()
