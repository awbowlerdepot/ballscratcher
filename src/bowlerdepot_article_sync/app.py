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
pros/cons, buying_tips, verdict, faq) into one HTML string. Only
action_shot_image_url (the 16:9 hero shot) is inlined as a plain
<img src="..."> tag pointing straight at its existing public S3 URL --
these are already served directly to the consumer site via public_api,
so there's no new hosting problem to solve. product_shot_image_url (the
1:1 square shot) is deliberately NOT embedded in the body -- Al's own
call: one hero image is enough in the post itself, and the square shot's
real job is now the WebDAV-uploaded thumbnail below.

**Thumbnail (thumbnail_path) -- built this session, Al's choice via
AskUserQuestion ("set up WebDAV properly" over the "skip it, rely on the
body image" fallback)**: BigCommerce's thumbnail_path can only point at
a file BigCommerce itself is already serving under /product_images/ --
never an arbitrary external URL -- so setting it requires a separate
upload via WebDAV, a COMPLETELY DIFFERENT credential type (a WebDAV
username [an email address] + password from Settings -> File access
(WebDAV) in BigCommerce's control panel) than the OAuth bearer token
(X-Auth-Token) used everywhere else in this module. upload_thumbnail_
via_webdav downloads the article's own action_shot_image_url and PUTs it
to {webdav_url}/product_images/uploaded_images/{filename} (that
uploaded_images/ convention is confirmed via a real BigCommerce CDN URL
seen during research:
.../product_images/uploaded_images/props.jpg), then thumbnail_path is
set to the path relative to /product_images/, i.e.
"uploaded_images/{filename}". Deliberately best-effort, same "degrade,
don't block" pattern as fetch_bigcommerce_product_url -- no WebDAV
credentials configured, or any failure during the download/upload, just
means the post goes out with no thumbnail_path key at all rather than
failing the whole article. get_bigcommerce_credentials reads the WebDAV
username/password/URL as three OPTIONAL extra keys (webdav_url/
webdav_username/webdav_password) on the SAME shared BIGCOMMERCE_SECRET_
ARN secret every other BigCommerce-touching Lambda here already reads --
deliberately not a new secret/parameter/IAM grant, since the ARN is
already wired everywhere it's needed. All three keys must be present or
webdav support is treated as "not configured yet" and skipped entirely.
Al adds them himself via `aws secretsmanager put-secret-value` (see
DEPLOY_RUNBOOK.md 6u) -- this sandbox never sees the actual WebDAV
password, same "Al supplies real credentials himself" pattern already
used for the OAuth token.

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

**Three invocation shapes, one handler** -- same "scheduled batch, plus
an on-demand single-item path checked first" convention product_article_
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
- `{"article_id": "...", "resync": true}` (admin_api's queue_article_
  resync, the Articles tab's "Resync" button -- added right after the
  WebDAV thumbnail fix, so Al could push a working thumbnail onto a post
  that had already gone out without one): scopes list_articles_needing_
  resync to that one row, and REQUIRES it to already have a bigcommerce_
  post_id/bowlerdepot_synced_at -- an article that was never synced has
  nothing to resync onto, so it goes through the normal Sync path
  instead. _process_one_article's resync branch PUTs the fresh body/
  thumbnail onto that SAME post id (push_article_update_to_bigcommerce)
  rather than POSTing a new one, so this overwrites the live post in
  place instead of creating a duplicate.
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
    a 403 from push_article_to_bigcommerce is the symptom if it doesn't.

    Also reads three OPTIONAL extra keys off the SAME secret --
    webdav_url/webdav_username/webdav_password -- for the thumbnail_path
    WebDAV upload (see this module's own docstring). Deliberately not a
    new secret/parameter: this ARN is already granted to this function,
    and a WebDAV username/password is a wholly different credential type
    than X-Auth-Token so it can't just be reused. Returns webdav=None
    unless all three keys are present, so an as-yet-unconfigured account
    degrades to "no thumbnail" rather than a KeyError."""
    import boto3

    secret_arn = os.environ["BIGCOMMERCE_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    secret = json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])

    webdav = None
    if secret.get("webdav_url") and secret.get("webdav_username") and secret.get("webdav_password"):
        webdav = {
            "url": secret["webdav_url"].rstrip("/"),
            "username": secret["webdav_username"],
            "password": secret["webdav_password"],
        }
    return secret["store_hash"], secret["auth_token"], webdav


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


def list_articles_needing_resync(conn, article_id: str) -> list:
    """Same row shape as list_articles_needing_sync, PLUS pa.bigcommerce_
    post_id (needed to target the update at the right existing post) --
    backs the "Resync" button (Al's follow-up ask right after the WebDAV
    thumbnail fix went live: an article synced BEFORE that fix has no
    thumbnail, and there was no way to push the fix onto an already-
    published post).

    Deliberately requires an explicit article_id -- unlike list_
    articles_needing_sync, there is no batch/scheduled resync mode.
    Resyncing overwrites a LIVE, already-published BigCommerce post, so
    this only ever runs as a deliberate one-at-a-time admin action, never
    unattended on a schedule.

    Conditions mirror list_articles_needing_sync (sync_to_bigcommerce =
    true, match_status = 'matched') but flipped on sync status: requires
    bowlerdepot_synced_at IS NOT NULL and bigcommerce_post_id IS NOT NULL
    -- an article that was never synced has nothing to resync onto, so
    it should go through the normal Sync path instead."""
    query = """
        select pa.id as article_id, pa.product_id, bp.bigcommerce_product_id,
               pa.bigcommerce_post_id,
               pa.title, pa.hook, pa.performance_summary, pa.who_should_buy,
               pa.who_should_skip, pa.pros, pa.cons, pa.buying_tips, pa.verdict,
               pa.faq, pa.action_shot_image_url, pa.product_shot_image_url,
               b.name as brand_name
        from product_articles pa
        join bowlerdepot_products bp on bp.product_id = pa.product_id
        join products p on p.id = pa.product_id
        join brands b on b.id = p.brand_id
        where pa.id = %s
          and pa.sync_to_bigcommerce = true
          and pa.bowlerdepot_synced_at is not null
          and pa.bigcommerce_post_id is not null
          and bp.match_status = 'matched'
    """
    with conn.cursor() as cur:
        cur.execute(query, [article_id])
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


def build_article_body_html(article: dict, product_url: str = None, thumbnail_path: str = None) -> str:
    """Pure function -- assembles product_articles' structured fields into
    one HTML string for the Blog Post's `body` field. See this module's
    own docstring for why the hero image is inlined by URL rather than
    uploaded via thumbnail_path.

    thumbnail_path, when given (the WebDAV upload succeeded -- see
    upload_thumbnail_via_webdav), means BigCommerce's own theme already
    renders that same action-shot image as the post's featured/thumbnail
    image at the top of the page -- **REAL INCIDENT (2026-09-05)**: once
    the WebDAV Digest-auth fix (see this module's own docstring) made
    thumbnail_path actually start working, Al immediately noticed the
    same image now showed up TWICE on a real published post, once as the
    theme's thumbnail and once again inline here. So the body only
    inlines the image itself as a FALLBACK, when there's no
    thumbnail_path (WebDAV not configured yet, or the upload failed) --
    otherwise the post would have no hero image at all in that case."""
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
    if article.get("action_shot_image_url") and not thumbnail_path:
        parts.append(f'<img src="{esc(article["action_shot_image_url"])}" alt="{esc(article.get("title"))}" />')
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


def upload_thumbnail_via_webdav(session, webdav: dict, article_id: str, image_url: str):
    """Best-effort: downloads the article's own action_shot_image_url
    (already public via S3/public_api) and re-uploads it to BigCommerce's
    WebDAV file store so it can be referenced as the Blog Post's
    thumbnail_path -- see this module's own docstring for why this
    separate upload is required at all. Returns None (never raises) on
    ANY failure -- missing/unconfigured webdav, a download error, or an
    upload error should all just mean "post without a thumbnail," not
    block the article, same "degrade, don't block" pattern as
    fetch_bigcommerce_product_url.

    Upload target is {webdav_url}/product_images/uploaded_images/
    {filename} (the uploaded_images/ convention is confirmed via a real
    BigCommerce CDN URL seen during research); the returned thumbnail_
    path is relative to /product_images/, i.e. "uploaded_images/
    {filename}". filename is derived from article_id (already a safe
    lowercase-hex-and-dashes UUID -- satisfies BigCommerce's documented
    a-z/0-9/-/_ filename restriction) plus the source image's own file
    extension (defaulting to .png if the URL's extension isn't a
    BigCommerce-recognized image type).

    **REAL INCIDENT (confirmed 2026-09-05, Al's first live test)**:
    BigCommerce's WebDAV is backed by SabreDAV, which flatly rejects
    plain HTTP Basic Auth -- a live `curl -v` reproduction with correct,
    Cyberduck-verified-working credentials got back 401 with
    `WWW-Authenticate: Digest realm="SabreDAV",qop="auth",...` and the
    literal SabreDAV error body "No 'Authorization: Digest' header
    found. Either the client didn't send one, or the server is
    misconfigured." Cyberduck (and every other real WebDAV client)
    performs the Digest challenge/response handshake transparently,
    which is exactly why "I just connected with Cyberduck, the
    credentials are fine" and "the Lambda gets 401" were BOTH true at
    the same time -- this was never a credentials problem. Fixed by
    using requests.auth.HTTPDigestAuth instead of a plain (user, pass)
    tuple (which requests treats as Basic Auth) -- HTTPDigestAuth
    performs the same two-round-trip handshake real WebDAV clients do."""
    if not webdav or not image_url:
        return None
    try:
        import requests

        image_resp = session.get(image_url, timeout=DEFAULT_FETCH_TIMEOUT_SECONDS)
        image_resp.raise_for_status()

        ext = image_url.rsplit(".", 1)[-1].lower().split("?")[0]
        if ext not in ("png", "jpg", "jpeg", "gif"):
            ext = "png"
        filename = f"article-{article_id}.{ext}"

        upload_resp = session.put(
            f"{webdav['url'].rstrip('/')}/product_images/uploaded_images/{filename}",
            data=image_resp.content,
            auth=requests.auth.HTTPDigestAuth(webdav["username"], webdav["password"]),
            timeout=DEFAULT_FETCH_TIMEOUT_SECONDS,
        )
        upload_resp.raise_for_status()
        return f"uploaded_images/{filename}"
    except Exception:
        logger.warning(
            "Could not upload WebDAV thumbnail for article_id=%s -- posting without a thumbnail_path",
            article_id, exc_info=True,
        )
        return None


def build_bigcommerce_blog_post_payload(article: dict, product_url: str = None, thumbnail_path: str = None) -> dict:
    """Maps one product_articles row to BigCommerce's documented Create
    Blog Post request body (confirmed live via their own docs this
    session): required title/body, plus is_published=true (posts default
    to draft otherwise) and tags=[brand_name] when known. author is set
    to "BowlerDepot" -- there's no per-article author concept in this
    schema, and leaving it blank is also a valid choice BigCommerce
    supports, but a named author reads better on a real blog.

    thumbnail_path is included only when the WebDAV upload actually
    succeeded (see upload_thumbnail_via_webdav) -- omitted entirely
    rather than sent as None/empty, so an unconfigured or failed upload
    still yields a perfectly valid, publishable post."""
    payload = {
        "title": article.get("title") or "",
        "body": build_article_body_html(article, product_url, thumbnail_path),
        "is_published": True,
        "author": "BowlerDepot",
        "tags": [article["brand_name"]] if article.get("brand_name") else [],
    }
    if thumbnail_path:
        payload["thumbnail_path"] = thumbnail_path
    return payload


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


def push_article_update_to_bigcommerce(session, store_hash: str, auth_token: str, bigcommerce_post_id, payload: dict) -> dict:
    """Resync path counterpart to push_article_to_bigcommerce: PUTs an
    update onto an ALREADY-published post instead of POSTing a new one,
    so a resync overwrites the live post (fresh body, fresh thumbnail_
    path attempt) rather than creating a duplicate. Same v2 API, same
    unwrapped-response shape (no "data" key) as the create path --
    confirmed against BigCommerce's own docs, which document PUT
    /stores/{store_hash}/v2/blog/posts/{id} as the update operation for
    this same v2 Blog Posts resource. Raises on a non-2xx response, same
    as push_article_to_bigcommerce -- caught per-article by
    _process_one_article."""
    resp = session.put(
        f"{BIGCOMMERCE_API_BASE}/stores/{store_hash}/v2/blog/posts/{bigcommerce_post_id}",
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


def _process_one_article(session, conn, store_hash: str, auth_token: str, article: dict, webdav: dict = None,
                          resync: bool = False) -> dict:
    """Pushes one article and records the result -- exceptions are caught
    HERE (not left to propagate) so handler's loop can keep going through
    the rest of a scheduled batch after one article's push fails (e.g. a
    403 from a not-yet-upgraded OAuth scope, or a stale
    bigcommerce_product_id). webdav is None when no WebDAV credentials
    are configured yet -- upload_thumbnail_via_webdav handles that (and
    every other failure mode) by just returning None, so this still
    posts a perfectly valid article without a thumbnail_path.

    resync=True (the "Resync" button, article came from list_articles_
    needing_resync so it's guaranteed to already have a bigcommerce_
    post_id) PUTs the freshly-rebuilt body/thumbnail_path onto that SAME
    existing post via push_article_update_to_bigcommerce, instead of
    POSTing a brand new one -- this is what actually lets an
    already-published post pick up a thumbnail that failed (or didn't
    exist yet) the first time around, without creating a duplicate post
    on the blog."""
    try:
        product_url = fetch_bigcommerce_product_url(
            session, store_hash, auth_token, article["bigcommerce_product_id"],
        )
        thumbnail_path = upload_thumbnail_via_webdav(
            session, webdav, article["article_id"], article.get("action_shot_image_url"),
        )
        payload = build_bigcommerce_blog_post_payload(article, product_url, thumbnail_path)
        if resync:
            push_article_update_to_bigcommerce(session, store_hash, auth_token, article["bigcommerce_post_id"], payload)
            bigcommerce_post_id = article["bigcommerce_post_id"]
        else:
            created = push_article_to_bigcommerce(session, store_hash, auth_token, payload)
            bigcommerce_post_id = created["id"]
        mark_article_synced(conn, article["article_id"], bigcommerce_post_id)
        return {"article_id": str(article["article_id"]), "success": True, "bigcommerce_post_id": bigcommerce_post_id}
    except Exception as exc:
        logger.exception(
            "Failed to %s article %s (product %s, bigcommerce_product_id %s) to BigCommerce",
            "resync" if resync else "push", article.get("article_id"), article.get("product_id"),
            article.get("bigcommerce_product_id"),
        )
        return {"article_id": str(article.get("article_id")), "success": False, "error": str(exc)}


def handler(event, context):
    """See this module's own docstring for the invocation shapes:
    scheduled batch (no article_id), on-demand single sync
    ({"article_id": "..."}), and on-demand resync
    ({"article_id": "...", "resync": true})."""
    import requests

    article_id = (event or {}).get("article_id")
    resync = bool((event or {}).get("resync"))
    conn = get_db_connection()
    try:
        if resync:
            if not article_id:
                logger.warning("resync=true requires an article_id -- nothing to do")
                return {"statusCode": 400, "body": json.dumps({"error": "resync requires an article_id"})}
            articles = list_articles_needing_resync(conn, article_id)
        else:
            articles = list_articles_needing_sync(conn, article_id=article_id)

        if not articles:
            logger.info("No articles need %s (article_id=%s)", "resyncing" if resync else "syncing", article_id)
            return {"statusCode": 200, "body": json.dumps({"pushed": 0, "failed": 0, "results": []})}

        store_hash, auth_token, webdav = get_bigcommerce_credentials()
        session = requests.Session()

        results = [
            _process_one_article(session, conn, store_hash, auth_token, article, webdav, resync=resync)
            for article in articles
        ]

        pushed = sum(1 for r in results if r["success"])
        failed = len(results) - pushed
        logger.info("BigCommerce article %s: %d pushed, %d failed", "resync" if resync else "sync", pushed, failed)
        return {"statusCode": 200, "body": json.dumps({"pushed": pushed, "failed": failed, "results": results})}
    finally:
        conn.close()
