"""
Writes a plain, server-rendered link to each ball's Learn review into that
ball's bowlerdepot.com product description -- Al: "add static links from
bowlerdepot.com to learn" (runbook 6fj), the follow-up to the Learn
indexing investigation in 6fi.

Why this exists when the product page already links to Learn: the only
link today is the "Learn" tab bowlerdepot-article-hero.js injects
(BigCommerce Script Manager, runbook 6ar). That tab is built in the
browser from a public_api fetch after page load, so it isn't in the HTML
BigCommerce serves. Google does render JavaScript, but later and less
reliably than it reads the HTML, and an injected link is a weak discovery
signal for a young subdomain with few backlinks. Checked live
(curl as Googlebot) on bowlerdepot.com/storm-ion-pro-pearl/: the only
"learn.bowlerdepot.com" in the served HTML is the <script src> itself,
while the theme's #tab-description content IS server-rendered straight
from the product's BigCommerce `description` field. So a link written
into that field is a real <a href> on every matched product page, on a
domain Google already crawls often.

What it writes: one paragraph, appended to the end of the description --

    <p class="bowleriq-learn-link"><a href="https://learn.bowlerdepot.com/articles/<slug>/">Read our full Storm Ion Pro Pearl review on BowlerDepot Learn</a></p>

The class is the marker. Every run strips any existing marked paragraph
before deciding what the description should be, so the job is
idempotent (a description that already ends in the right paragraph is
left alone, no PUT), a changed slug or name rewrites the paragraph in
place, and a ball whose article is no longer approved (or that's been
unpublished) has its paragraph removed. A class rather than an HTML
comment because BigCommerce's control-panel editor (TinyMCE) drops
comments when staff re-save a description, but keeps class attributes;
if an edit does drop the class too, the worst case is one duplicate
link until someone deletes the stale one -- never a lost description.
Nothing else in the description is touched: the job only ever removes
its own marked paragraph and appends a new one.

The URL ends in "/" because Learn now 301s the slash-less form (6fi);
linking straight to the canonical URL avoids spending a redirect.

Which products: the same gate public_api.get_article_hero_by_
bigcommerce_product_id uses for the injected tab -- match_status =
'matched' (bowlerdepot_reconciliation's confidence gate; never write onto
an 'ambiguous'/'unmatched' product, see the Storm iQ Tour incident,
6h.1), products.published, product_articles.status = 'approved'. Every
matched BigCommerce product is checked, not only the ones with an
article, so removals happen too.

Reads are one bulk GET per 250 products (Catalog v3 `id:in` +
`include_fields=description`); writes are one PUT per changed product.
A daily run with nothing changed costs one GET.

Prerequisite (NOT something this code can fix): the PUT needs the
Products "modify" OAuth scope (store_v2_products) on the token behind
BIGCOMMERCE_SECRET_ARN. As of 2026-10-30 that token is read-only -- every
bowlerdepot_video_sync push 403s (1,607 failed on the 2026-10-31 02:46
UTC run) -- so this job's PUTs will 403 the same way until Al upgrades
the token in BigCommerce's API Accounts settings. A 403 is logged per
product and the run carries on. {"dry_run": true} reads and plans
without writing, which works with the read-only token today.
"""
import html
import json
import logging
import os
import re

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BIGCOMMERCE_API_BASE = "https://api.bigcommerce.com"
DEFAULT_FETCH_TIMEOUT_SECONDS = 30
LEARN_SITE_URL = "https://learn.bowlerdepot.com"
# BigCommerce's Catalog v3 list endpoint caps `limit` at 250.
GET_BATCH_SIZE = 250
MARKER_CLASS = "bowleriq-learn-link"

# Matches our own paragraph (and the whitespace before it) wherever it
# sits. Non-greedy to the first </p>, and the paragraph we write never
# nests one, so this can't eat into the store's own copy.
_MARKED_PARAGRAPH_RE = re.compile(
    r"\s*<p\b[^>]*\bclass=\"[^\"]*\b" + re.escape(MARKER_CLASS) + r"\b[^\"]*\"[^>]*>.*?</p>",
    re.IGNORECASE | re.DOTALL,
)


def get_bigcommerce_credentials():
    """Identical to bowlerdepot_video_sync.get_bigcommerce_credentials --
    duplicated, not shared, same "each Lambda owns its own deploy
    package" convention as the rest of this codebase. The PUT needs the
    Products "modify" scope; see the module docstring."""
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


def list_matched_products(conn) -> list:
    """Every confidently-matched BigCommerce product, with its Learn
    article's slug when it has one public_api would also show (published
    ball, approved article -- see the module docstring for why this is
    the hero embed's exact gate). slug is null for a matched product
    with no showable article: that row is still returned so a previously
    written link gets removed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select bp.bigcommerce_product_id, b.name as brand_name, p.name as product_name,
                   case when p.published and pa.status = 'approved' then pa.slug end as slug
            from bowlerdepot_products bp
            join products p on p.id = bp.product_id
            left join brands b on b.id = p.brand_id
            left join product_articles pa on pa.product_id = p.id
            where bp.match_status = 'matched'
              and bp.bigcommerce_product_id is not null
            order by bp.bigcommerce_product_id
            """
        )
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def ball_display_name(brand_name, product_name: str) -> str:
    """"Storm Ion Pro Pearl" -- brand first, as the Learn article titles
    and the storefront both say it, unless the product name already
    starts with the brand."""
    name = (product_name or "").strip()
    brand = (brand_name or "").strip()
    if brand and not name.lower().startswith(brand.lower()):
        return f"{brand} {name}"
    return name


def build_link_paragraph(slug: str, display_name: str) -> str:
    url = f"{LEARN_SITE_URL}/articles/{slug}/"
    return (
        f'<p class="{MARKER_CLASS}"><a href="{html.escape(url, quote=True)}">'
        f"Read our full {html.escape(display_name)} review on BowlerDepot Learn</a></p>"
    )


def desired_description(current: str, link_paragraph) -> str:
    """Pure: the description with our marked paragraph removed, then (if
    link_paragraph) appended once at the end. Applying it to its own
    output returns the same string, which is what makes a re-run a
    no-op."""
    base = _MARKED_PARAGRAPH_RE.sub("", current or "").rstrip()
    if not link_paragraph:
        return base
    return f"{base}\n{link_paragraph}" if base else link_paragraph


def fetch_descriptions(session, store_hash: str, auth_token: str, product_ids: list) -> dict:
    """{bigcommerce_product_id (str): description} for every id that
    still exists in BigCommerce. A deleted product just isn't in the
    response, and the caller skips it."""
    out = {}
    for i in range(0, len(product_ids), GET_BATCH_SIZE):
        chunk = product_ids[i:i + GET_BATCH_SIZE]
        resp = session.get(
            f"{BIGCOMMERCE_API_BASE}/stores/{store_hash}/v3/catalog/products",
            headers={"Accept": "application/json", "X-Auth-Token": auth_token},
            params={
                "id:in": ",".join(chunk),
                "include_fields": "description",
                "limit": GET_BATCH_SIZE,
            },
            timeout=DEFAULT_FETCH_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        for item in resp.json().get("data", []):
            out[str(item["id"])] = item.get("description") or ""
    return out


def put_description(session, store_hash: str, auth_token: str,
                    bigcommerce_product_id: str, description: str) -> None:
    resp = session.put(
        f"{BIGCOMMERCE_API_BASE}/stores/{store_hash}/v3/catalog/products/{bigcommerce_product_id}",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Auth-Token": auth_token,
        },
        json={"description": description},
        timeout=DEFAULT_FETCH_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


def plan_changes(products: list, descriptions: dict) -> list:
    """Pure: [{bigcommerce_product_id, action, description}] for every
    product whose description needs to change. action is 'add' (no link
    yet), 'update' (a link that's out of date) or 'remove' (no showable
    article any more)."""
    changes = []
    for row in products:
        bc_id = str(row["bigcommerce_product_id"])
        if bc_id not in descriptions:
            continue
        current = descriptions[bc_id]
        paragraph = (
            build_link_paragraph(row["slug"], ball_display_name(row["brand_name"], row["product_name"]))
            if row.get("slug") else None
        )
        new = desired_description(current, paragraph)
        if new == current:
            continue
        had_link = bool(_MARKED_PARAGRAPH_RE.search(current))
        action = "remove" if not paragraph else ("update" if had_link else "add")
        changes.append({"bigcommerce_product_id": bc_id, "action": action, "description": new})
    return changes


def handler(event, context):
    """Daily (template.yaml) or on demand. {"dry_run": true} returns the
    plan (ids and actions, not the descriptions themselves) without
    writing anything."""
    import requests

    dry_run = bool((event or {}).get("dry_run"))
    conn = get_db_connection()
    try:
        products = list_matched_products(conn)
    finally:
        conn.close()

    # One row per BigCommerce product: the reconciliation never maps two
    # of our products onto one BigCommerce product today, but if it ever
    # did, prefer the row that has an article so a link isn't added and
    # removed in the same run.
    by_id = {}
    for row in products:
        bc_id = str(row["bigcommerce_product_id"])
        if bc_id not in by_id or (row.get("slug") and not by_id[bc_id].get("slug")):
            by_id[bc_id] = row
    products = list(by_id.values())

    store_hash, auth_token = get_bigcommerce_credentials()
    session = requests.Session()
    descriptions = fetch_descriptions(session, store_hash, auth_token, list(by_id.keys()))
    changes = plan_changes(products, descriptions)

    summary = {
        "matched": len(products),
        "found_in_bigcommerce": len(descriptions),
        "with_article": sum(1 for p in products if p.get("slug")),
        "planned": {a: sum(1 for c in changes if c["action"] == a) for a in ("add", "update", "remove")},
        "dry_run": dry_run,
    }
    if dry_run:
        summary["changes"] = [{k: c[k] for k in ("bigcommerce_product_id", "action")} for c in changes]
        logger.info("Learn link sync (dry run): %s", json.dumps(summary["planned"]))
        return {"statusCode": 200, "body": json.dumps(summary)}

    written, failed = 0, []
    for change in changes:
        try:
            put_description(session, store_hash, auth_token, change["bigcommerce_product_id"], change["description"])
            written += 1
        except Exception as exc:
            logger.exception("Failed to write Learn link for BigCommerce product %s", change["bigcommerce_product_id"])
            failed.append({"bigcommerce_product_id": change["bigcommerce_product_id"], "error": str(exc)})

    summary.update({"written": written, "failed": len(failed), "failures": failed[:20]})
    logger.info("Learn link sync: %d written, %d failed of %d planned", written, len(failed), len(changes))
    return {"statusCode": 200, "body": json.dumps(summary)}
