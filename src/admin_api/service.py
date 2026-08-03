"""
Business logic for the admin approval API, kept separate from the FastAPI
route layer in app.py so it's testable without fastapi/pydantic installed
(neither was installable in this sandbox -- pip's proxy returned 403 for
every attempt, same restriction noted in the other modules' README
caveats). Everything in this file is plain functions + a psycopg2
connection; app.py is a thin routing wrapper around it.

Covers the workflow the architecture doc decided on: review everything on
the initial catalog load, then steady-state auto-approve except mismatched
fields written to review_queue by the scraping functions (currently just
pdf_parser.sync_pdf_skus; bowwwl.com cross-check and BowlerDepot
reconciliation are meant to write into the same table later, not built
yet). Approving a review_queue row applies its proposed_value to the real
column it describes; rejecting leaves the stored value untouched.
"""
import re

# review_queue.field_name convention, established by pdf_parser.sync_pdf_skus:
# a per-weight SKU field looks like "rg_16lb" / "differential_15lb" /
# "mass_bias_12lb"; anything else is treated as a product-level field name,
# but ONLY if it's in this whitelist -- field_name ultimately becomes a SQL
# column name, so an unrecognized value must be rejected rather than used
# directly (this is the injection guard, not string-escaping).
SKU_FIELD_NAME_RE = re.compile(r"^(rg|differential|mass_bias)_(\d{1,2})lb$")

PRODUCT_UPDATABLE_FIELDS = {
    "name", "color", "core_name", "coverstock_material", "coverstock_type",
    "coverstock_name", "factory_finish", "part_number", "published",
}

# Fields on product_skus that are numeric -- proposed_value is stored as
# text on review_queue (it has to represent values from multiple sources
# uniformly), so these need casting before being written back.
NUMERIC_SKU_FIELDS = {"rg", "differential", "mass_bias"}


def parse_review_field_name(field_name: str) -> dict:
    """Decides what a review_queue.field_name actually refers to. Returns
    {"scope": "sku", "column": "rg", "weight_lbs": 16} or
    {"scope": "product", "column": "color"}. Raises ValueError for
    anything not recognized -- deliberately fails closed rather than
    guessing, since this drives which SQL column gets written."""
    sku_match = SKU_FIELD_NAME_RE.match(field_name)
    if sku_match:
        return {"scope": "sku", "column": sku_match.group(1), "weight_lbs": int(sku_match.group(2))}

    if field_name in PRODUCT_UPDATABLE_FIELDS:
        return {"scope": "product", "column": field_name}

    raise ValueError(f"Unrecognized or non-updatable review_queue field_name: {field_name!r}")


def _cast_proposed_value(column: str, raw_value):
    if raw_value is None:
        return None
    if column in NUMERIC_SKU_FIELDS:
        return float(raw_value)
    if column == "published":
        return str(raw_value).strip().lower() in ("true", "t", "1", "yes")
    return raw_value


def build_update_plan(field_name: str, proposed_value) -> dict:
    """Pure decision of what to write where -- no DB access, fully testable.
    Returns a plan dict consumed by execute_update_plan(). Raises ValueError
    via parse_review_field_name for unrecognized fields."""
    parsed = parse_review_field_name(field_name)
    value = _cast_proposed_value(parsed["column"], proposed_value)

    if parsed["scope"] == "sku":
        return {
            "table": "product_skus",
            "column": parsed["column"],
            "value": value,
            "where": {"weight_lbs": parsed["weight_lbs"]},  # product_id added by caller
        }
    return {
        "table": "products",
        "column": parsed["column"],
        "value": value,
        "where": {},  # product_id (the row id itself) added by caller
    }


def execute_update_plan(cur, product_id: str, plan: dict) -> None:
    """Applies a build_update_plan() result via the given cursor. Column
    names are only ever drawn from SKU_FIELD_NAME_RE's fixed group or
    PRODUCT_UPDATABLE_FIELDS, both closed whitelists -- never from
    unsanitized user input -- so building the column name into the SQL
    string here is safe; the value itself is always parameterized."""
    if plan["table"] == "product_skus":
        cur.execute(
            f"update product_skus set {plan['column']} = %s, updated_at = now() "
            f"where product_id = %s and weight_lbs = %s",
            (plan["value"], product_id, plan["where"]["weight_lbs"]),
        )
    else:
        cur.execute(
            f"update products set {plan['column']} = %s, updated_at = now() where id = %s",
            (plan["value"], product_id),
        )


# ---------------------------------------------------------------------
# DB access. Deferred-imported psycopg2, mirroring the other functions --
# untested in this sandbox for the same reason noted in their READMEs
# (no Postgres instance available to actually run against). The functions
# above (parse_review_field_name, build_update_plan) are the part that's
# unit tested -- see tests/test_admin_api_service.py.
# ---------------------------------------------------------------------

import json
import os


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


def list_review_queue(conn, status: str = "pending", product_id: str = None, limit: int = 50, offset: int = 0) -> list:
    query = """
        select rq.id, rq.product_id, p.name as product_name, p.url as product_url,
               rq.field_name, rq.current_value, rq.proposed_value, rq.source,
               rq.reason, rq.status, rq.created_at, rq.resolved_at, rq.resolved_by
        from review_queue rq
        join products p on p.id = rq.product_id
        where rq.status = %s
    """
    params = [status]
    if product_id:
        query += " and rq.product_id = %s"
        params.append(product_id)
    query += " order by rq.created_at asc limit %s offset %s"
    params += [limit, offset]

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_review_item(conn, review_id: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            select rq.id, rq.product_id, p.name as product_name, p.url as product_url,
                   rq.field_name, rq.current_value, rq.proposed_value, rq.source,
                   rq.reason, rq.status, rq.created_at, rq.resolved_at, rq.resolved_by
            from review_queue rq
            join products p on p.id = rq.product_id
            where rq.id = %s
            """,
            (review_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cur.description]
        return dict(zip(columns, row))


def approve_review_item(conn, review_id: str, resolved_by: str) -> dict:
    """Applies the review item's proposed_value to the real column it
    describes, then marks the row approved. Raises ValueError (via
    build_update_plan) if field_name isn't recognized -- the row is left
    pending in that case so it doesn't silently vanish from the queue."""
    item = get_review_item(conn, review_id)
    if item is None:
        raise LookupError(f"No review_queue row with id {review_id}")
    if item["status"] != "pending":
        raise ValueError(f"review_queue row {review_id} is already {item['status']}, not pending")

    plan = build_update_plan(item["field_name"], item["proposed_value"])

    with conn.cursor() as cur:
        execute_update_plan(cur, item["product_id"], plan)
        cur.execute(
            "update review_queue set status = 'approved', resolved_at = now(), resolved_by = %s where id = %s",
            (resolved_by, review_id),
        )
    conn.commit()

    return {"review_id": review_id, "status": "approved", "applied": plan}


def reject_review_item(conn, review_id: str, resolved_by: str, reason: str = None) -> dict:
    """Marks the row rejected without touching the underlying data --
    the current stored value is presumed correct, the proposed one is
    discarded."""
    with conn.cursor() as cur:
        cur.execute("select status from review_queue where id = %s", (review_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No review_queue row with id {review_id}")
        if row[0] != "pending":
            raise ValueError(f"review_queue row {review_id} is already {row[0]}, not pending")

        note = f"Rejected: {reason}" if reason else "Rejected"
        cur.execute(
            "update review_queue set status = 'rejected', resolved_at = now(), resolved_by = %s, "
            "reason = coalesce(reason || ' | ', '') || %s where id = %s",
            (resolved_by, note, review_id),
        )
    conn.commit()
    return {"review_id": review_id, "status": "rejected"}


def get_pending_review_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("select count(*) from review_queue where status = 'pending'")
        return cur.fetchone()[0]


def list_products(conn, published: bool = None, brand_id: str = None, search: str = None, limit: int = 50, offset: int = 0) -> list:
    query = "select id, brand_id, name, url, status, published, updated_at from products where 1=1"
    params = []
    if published is not None:
        query += " and published = %s"
        params.append(published)
    if brand_id:
        query += " and brand_id = %s"
        params.append(brand_id)
    if search:
        query += " and name ilike %s"
        params.append(f"%{search}%")
    query += " order by updated_at desc limit %s offset %s"
    params += [limit, offset]

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_product(conn, product_id: str):
    with conn.cursor() as cur:
        cur.execute("select * from products where id = %s", (product_id,))
        row = cur.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cur.description]
        product = dict(zip(columns, row))

        cur.execute(
            "select weight_lbs, rg, differential, mass_bias, source, needs_review "
            "from product_skus where product_id = %s order by weight_lbs desc",
            (product_id,),
        )
        sku_columns = [desc[0] for desc in cur.description]
        product["skus"] = [dict(zip(sku_columns, row)) for row in cur.fetchall()]

        cur.execute(
            "select image_type, weight_lbs_context, source_url, stored_url "
            "from product_images where product_id = %s",
            (product_id,),
        )
        image_columns = [desc[0] for desc in cur.description]
        product["images"] = [dict(zip(image_columns, row)) for row in cur.fetchall()]

        return product


def set_product_published(conn, product_id: str, published: bool) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "update products set published = %s, updated_at = now() where id = %s returning id",
            (published, product_id),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product with id {product_id}")
    conn.commit()
    return {"product_id": product_id, "published": published}


# ---------------------------------------------------------------------
# Video candidates (YouTube content enrichment). Same approve/reject shape
# as review_queue above, but a dedicated table -- see
# db/migrations/004_product_videos.sql's comment for why product_videos
# isn't just reusing review_queue's field_name/current_value/proposed_value
# shape. Approving a candidate here has a side effect the review_queue
# approve doesn't: it publishes an SQS message so video_summarizer picks up
# the transcript+Bedrock-summary work, rather than applying a value
# directly.
# ---------------------------------------------------------------------

def list_video_candidates(conn, status: str = "pending", product_id: str = None, limit: int = 50, offset: int = 0) -> list:
    query = """
        select pv.id, pv.product_id, p.name as product_name, b.name as brand_name,
               pv.youtube_video_id, pv.title, pv.channel_title, pv.published_at,
               pv.thumbnail_url, pv.match_query, pv.match_confidence,
               pv.transcript_note, pv.status, pv.source,
               pv.created_at, pv.resolved_at, pv.resolved_by,
               (pv.summary is not null) as has_summary
        from product_videos pv
        join products p on p.id = pv.product_id
        join brands b on b.id = p.brand_id
        where pv.status = %s
    """
    params = [status]
    if product_id:
        query += " and pv.product_id = %s"
        params.append(product_id)
    query += " order by pv.match_confidence asc, pv.created_at asc limit %s offset %s"
    params += [limit, offset]

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_video_candidate(conn, video_id: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            select pv.*, p.name as product_name, b.name as brand_name
            from product_videos pv
            join products p on p.id = pv.product_id
            join brands b on b.id = p.brand_id
            where pv.id = %s
            """,
            (video_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cur.description]
        return dict(zip(columns, row))


def get_pending_video_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("select count(*) from product_videos where status = 'pending'")
        return cur.fetchone()[0]


def approve_video_candidate(conn, video_id: str, resolved_by: str) -> dict:
    """Marks the candidate approved, then best-effort publishes it to
    VIDEO_SUMMARIZE_QUEUE_URL for video_transcript_fetcher to pick up.
    Mirrors the other functions' "if configured" guard (see e.g.
    netsuite_url_discovery.handler's queue_url check) -- if the queue isn't
    wired up yet (env var unset), the row is still approved, it just won't
    be enriched until re-approved or the queue's added, rather than failing
    the whole request.

    Also selects youtube_video_id here and forwards it in the published
    message: video_transcript_fetcher (the queue's consumer as of the
    split-architecture change) is deliberately NOT VPC-attached and has no
    DB access, so it can't look this up itself -- it needs the id handed to
    it directly."""
    with conn.cursor() as cur:
        cur.execute("select status, youtube_video_id from product_videos where id = %s", (video_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_videos row with id {video_id}")
        if row[0] != "pending":
            raise ValueError(f"product_videos row {video_id} is already {row[0]}, not pending")
        youtube_video_id = row[1]

        cur.execute(
            "update product_videos set status = 'approved', resolved_at = now(), resolved_by = %s where id = %s",
            (resolved_by, video_id),
        )
    conn.commit()

    queued = _publish_video_summarize_message(video_id, youtube_video_id)
    return {"video_id": video_id, "status": "approved", "queued_for_summary": queued}


def reject_video_candidate(conn, video_id: str, resolved_by: str, reason: str = None) -> dict:
    with conn.cursor() as cur:
        cur.execute("select status from product_videos where id = %s", (video_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_videos row with id {video_id}")
        if row[0] != "pending":
            raise ValueError(f"product_videos row {video_id} is already {row[0]}, not pending")

        cur.execute(
            "update product_videos set status = 'rejected', resolved_at = now(), resolved_by = %s where id = %s",
            (resolved_by, video_id),
        )
    conn.commit()
    return {"video_id": video_id, "status": "rejected"}


def _publish_video_summarize_message(product_video_id: str, youtube_video_id: str) -> bool:
    queue_url = os.environ.get("VIDEO_SUMMARIZE_QUEUE_URL")
    if not queue_url:
        return False

    import boto3

    sqs = boto3.client("sqs")
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({
            "product_video_id": product_video_id,
            "youtube_video_id": youtube_video_id,
        }),
    )
    return True
