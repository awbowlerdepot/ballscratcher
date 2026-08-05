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
    "name", "color", "coverstock_material", "coverstock_type",
    "coverstock_name", "factory_finish", "part_number", "published",
}
# core_name removed (migration 007): it was never actually a products
# column -- only ball_families (now cores) ever had it, and that table was
# never wired up either, so this was a latent bug that would have 500'd
# execute_update_plan's f-string UPDATE if any review_queue row had ever
# actually carried field_name="core_name" (confirmed via grep that nothing
# ever wrote one -- bowwwl_cross_check explicitly excludes core from its
# comparable fields). Core info now lives on the cores table, joined in via
# get_product() below rather than being a directly-editable product field.

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


def list_products(conn, published: bool = None, brand_id: str = None, search: str = None,
                   needs_video_summary_refresh: bool = None, has_approved_video_summaries: bool = None,
                   limit: int = 50, offset: int = 0) -> list:
    """needs_video_summary_refresh=True: products with at least one
    approved+summarized video, where video_reviews_summary is either
    still unset or stale relative to how many approved+summarized videos
    currently exist (video_reviews_summary_video_count is stored exactly
    for this comparison -- see 006_products_video_reviews_summary.sql).
    Built for scripts/backfill_video_review_rollups.py, but written as a
    general filter (not a one-off) since the same staleness can recur
    later -- a reassign/delete cleanup, or a product summarized before
    video_summarizer's automatic regeneration existed.

    has_approved_video_summaries=True: every product with at least one
    approved+summarized video, full stop -- a superset of
    needs_video_summary_refresh, no staleness comparison at all. Added
    once products.description started getting backfilled onto already-
    scraped products (see parse_description in the four *_product_
    scraper modules): a description change doesn't move the approved+
    summarized video count, so needs_video_summary_refresh's staleness
    check has no way to notice a product's rollup could now say more with
    the new context available -- by that filter's definition, a rollup
    already built from the right number of videos IS current. This filter
    is the deliberately-blunt "just regenerate everything" escape hatch
    for that case (or any other "the inputs changed in a way the count-
    based check can't see" situation), meant for an occasional one-time
    catalog-wide pass (see scripts/backfill_video_review_rollups.py's
    REFRESH_ALL), not routine/scheduled use the way needs_video_summary_
    refresh is."""
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
    if needs_video_summary_refresh:
        query += """
            and exists (
                select 1 from product_videos pv
                where pv.product_id = products.id and pv.status = 'approved' and pv.summary is not null
            )
            and (
                video_reviews_summary is null
                or video_reviews_summary_video_count <> (
                    select count(*) from product_videos pv2
                    where pv2.product_id = products.id and pv2.status = 'approved' and pv2.summary is not null
                )
            )
        """
    if has_approved_video_summaries:
        query += """
            and exists (
                select 1 from product_videos pv
                where pv.product_id = products.id and pv.status = 'approved' and pv.summary is not null
            )
        """
    # id as a final tiebreaker -- same reason list_video_candidates and
    # fetch_products_to_search needed one (see admin_api/service.py's own
    # earlier fix and video_discovery/app.py's ROTATION section): rows
    # sharing an updated_at value make plain OFFSET/LIMIT pagination
    # unstable, and this endpoint is now paginated by a real consumer
    # (the backfill script) as of this filter's addition.
    query += " order by updated_at desc, id asc limit %s offset %s"
    params += [limit, offset]

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_product(conn, product_id: str):
    with conn.cursor() as cur:
        # Left join cores (migration 007) so the detail view can show a
        # human-readable core name/type alongside p.core_id -- p.* still
        # carries core_id itself, c.name/c.core_type are the added columns.
        cur.execute(
            """
            select p.*, c.name as core_name, c.core_type as core_type
            from products p
            left join cores c on c.id = p.core_id
            where p.id = %s
            """,
            (product_id,),
        )
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
    """Real bug found via a live full-catalog run of
    scripts/auto_approve_video_candidates.py: a single video_discovery
    invocation inserts many product_videos rows in quick succession, often
    with identical or near-identical `created_at` timestamps -- ordering
    by (match_confidence, created_at) alone has no way to break those
    ties deterministically, so OFFSET/LIMIT pagination across multiple
    calls (this function is paginated by both that script and
    home_transcript_fetcher.py) could return the same row on two different
    pages (observed live: one candidate got approved, then failed with a
    real 422 the second time it showed up) and, by the same instability,
    could just as easily have skipped a different row entirely without any
    visible error. `pv.id` is added as a final, always-unique tiebreaker so
    the ordering -- and therefore the pagination -- is fully deterministic."""
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
    query += " order by pv.match_confidence asc, pv.created_at asc, pv.id asc limit %s offset %s"
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
    """Marks the candidate approved. Deliberately does NOT publish to
    VIDEO_SUMMARIZE_QUEUE_URL / video_transcript_fetcher anymore -- that
    was this function's original behavior, but it created a real race once
    the home browser fetcher (scripts/home_transcript_fetcher_browser.py)
    became the confirmed-working transcript path: video_transcript_fetcher
    is a plain-HTTP fetch and, per its own module docstring, is confirmed
    blocked by YouTube's PoToken/BotGuard requirement regardless of network
    path (VPC or not). It never raises on that -- get_transcript treats a
    blocked fetch as an expected outcome and always forwards a result with
    a transcript_note (e.g. "captions_listed_but_transcript_fetch_returned_
    empty") to video_summarizer, which writes that note onto the row.
    needs_transcript() (home_transcript_fetcher.py) only picks up rows
    where transcript_note IS NULL -- so if this function still auto-queued
    to that broken Lambda on every approval, every candidate would get a
    failure note written moments after approval, and the actually-working
    browser fetcher would skip it forever, having no way to tell "genuinely
    checked and no captions" apart from "never got a real attempt". Leaving
    transcript_note untouched at approval time is what lets the home
    browser cron (the confirmed-working path, see DEPLOY_RUNBOOK.md 6k) be
    the one and only thing that sets it. VideoTranscriptFetcherFunction and
    VideoSummarizeQueue are left deployed (nothing publishes to the queue
    now, so the function simply never fires) rather than torn out here --
    removing dead infra is a separate, lower-stakes cleanup, not bundled
    into this fix.

    Also selects youtube_video_id here (unused for publishing now, but kept
    since callers/tests may still want it, and it costs nothing extra in
    the same query)."""
    with conn.cursor() as cur:
        cur.execute("select status, youtube_video_id from product_videos where id = %s", (video_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_videos row with id {video_id}")
        if row[0] != "pending":
            raise ValueError(f"product_videos row {video_id} is already {row[0]}, not pending")

        cur.execute(
            "update product_videos set status = 'approved', resolved_at = now(), resolved_by = %s where id = %s",
            (resolved_by, video_id),
        )
    conn.commit()

    return {"video_id": video_id, "status": "approved"}


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


def reassign_video_candidate(conn, video_id: str, new_product_id: str) -> dict:
    """Moves a video candidate to a different product. Built for a real,
    known failure mode of video_discovery's score_match heuristic (see its
    module docstring): 'high' confidence only requires the brand name plus
    ANY ONE significant product-name token in the title, so e.g. "Storm
    Absolute Power Review" scores 'high' for the "Storm Absolute" product
    too, not just "Storm Absolute Power" -- a real, accepted tradeoff of
    auto-approving 'high' matches in bulk (see
    scripts/auto_approve_video_candidates.py's docstring), not something
    this function tries to prevent. This is the correction tool for when
    it happens: works regardless of status (pending/approved/rejected),
    and deliberately does NOT touch transcript/transcript_note/summary --
    if a transcript was already fetched under the wrong product, it's
    still a real transcript of that same YouTube video, no reason to lose
    it and refetch under the correct product.

    Checked, not caught: looks for an existing (new_product_id,
    youtube_video_id) row before updating, rather than attempting the
    update and catching a unique-constraint violation from the DB driver --
    a clearer, more actionable error this way ("one already exists over
    there, delete a duplicate first") than a raw IntegrityError, and this
    is an admin tool used by one person at a time, so the small
    check-then-act race window is an acceptable tradeoff. If that
    conflict fires, delete_video_candidate below is the cleanup path."""
    with conn.cursor() as cur:
        cur.execute("select id, youtube_video_id from product_videos where id = %s", (video_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_videos row with id {video_id}")
        youtube_video_id = row[1]

        cur.execute("select id from products where id = %s", (new_product_id,))
        if cur.fetchone() is None:
            raise LookupError(f"No products row with id {new_product_id}")

        cur.execute(
            "select id from product_videos where product_id = %s and youtube_video_id = %s",
            (new_product_id, youtube_video_id),
        )
        conflict = cur.fetchone()
        if conflict is not None:
            raise ValueError(
                f"product {new_product_id} already has a product_videos row for "
                f"youtube_video_id={youtube_video_id} (id={conflict[0]}) -- delete "
                f"one of the two duplicates first (see delete_video_candidate), "
                f"then retry the reassignment"
            )

        cur.execute("update product_videos set product_id = %s where id = %s", (new_product_id, video_id))
    conn.commit()
    return {"video_id": video_id, "product_id": new_product_id}


def delete_video_candidate(conn, video_id: str) -> dict:
    """Hard delete -- distinct from reject_video_candidate, which only
    marks status='rejected' and keeps the row for audit. Built as the
    cleanup step for reassign_video_candidate's conflict case above: two
    product_videos rows for the same (product_id, youtube_video_id) pair
    can't coexist (see 004_product_videos.sql's unique constraint), but two
    DIFFERENT products can each have their own row for the same YouTube
    video (a real, legitimate case -- one review video can genuinely cover
    two products), or a video can get moved to the correct product while a
    stale duplicate is left sitting under the wrong one. Deleting the
    wrong copy is a normal, expected cleanup action here, not a mistake to
    guard against with extra confirmation steps -- same one-admin-at-a-time
    reasoning as reassign_video_candidate above."""
    with conn.cursor() as cur:
        cur.execute("select id from product_videos where id = %s", (video_id,))
        if cur.fetchone() is None:
            raise LookupError(f"No product_videos row with id {video_id}")
        cur.execute("delete from product_videos where id = %s", (video_id,))
    conn.commit()
    return {"video_id": video_id, "deleted": True}


# _publish_video_summarize_message (published to VIDEO_SUMMARIZE_QUEUE_URL
# for video_transcript_fetcher) used to live here and was called from
# approve_video_candidate -- removed rather than left as dead code once
# approve_video_candidate stopped calling it (see that function's
# docstring for why). VIDEO_SUMMARIZE_QUEUE_URL/VideoSummarizeQueue are
# still wired in template.yaml; nothing publishes to that queue anymore,
# so VideoTranscriptFetcherFunction simply never fires. Tearing that infra
# out is a separate cleanup, not bundled into this fix.


def submit_video_transcript(conn, video_id: str, transcript: str, transcript_note: str = None) -> dict:
    """Publishes an externally-fetched transcript straight to
    VideoTranscriptResultQueue -- the same queue video_transcript_fetcher
    publishes to -- so video_summarizer picks it up and does the DB write +
    Bedrock call exactly like it would for a Lambda-fetched transcript, no
    special-casing downstream. This is the real reason this endpoint exists:
    live testing this session found YouTube's caption-fetch behavior
    identical (blocked) from both a VPC-attached and a non-VPC Lambda, but
    working from a residential connection -- see
    src/video_transcript_fetcher/app.py's module docstring for the full
    evidence trail, and scripts/home_transcript_fetcher.py for the
    residential-side counterpart that calls this endpoint, meant to run on
    the user's own hardware at home rather than in AWS.

    Only requires the row exist and already be 'approved' -- the same gate
    video_summarizer's own _process_one applies -- so this can't be used to
    inject a transcript onto a row that's still pending review or was
    rejected. Deliberately does NOT soft-fail like
    _publish_video_summarize_message does when its queue isn't configured
    (that function has a DB write to fall back on; this one's entire job
    IS the publish, there's nothing else to persist) -- a missing
    TRANSCRIPT_RESULT_QUEUE_URL is a real deployment misconfiguration, so
    it's left to raise (KeyError, surfaced as a 500) rather than silently
    discarding the caller's transcript."""
    with conn.cursor() as cur:
        cur.execute("select status from product_videos where id = %s", (video_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No product_videos row with id {video_id}")
        if row[0] != "approved":
            raise ValueError(
                f"product_videos row {video_id} is {row[0]}, not approved -- can't submit a transcript for it"
            )

    _publish_transcript_result_message(video_id, transcript, transcript_note)
    return {"video_id": video_id, "queued_for_summary": True}


def _publish_transcript_result_message(product_video_id: str, transcript: str, transcript_note: str) -> None:
    queue_url = os.environ["TRANSCRIPT_RESULT_QUEUE_URL"]

    import boto3

    sqs = boto3.client("sqs")
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({
            "product_video_id": product_video_id,
            "transcript": transcript,
            "transcript_note": transcript_note,
        }),
    )


# ---------------------------------------------------------------------
# "Summary of summaries" on-demand refresh (POST /products/{id}/refresh-
# video-summary). video_summarizer.refresh_video_reviews_rollup already
# regenerates products.video_reviews_summary automatically every time a
# video gets a real summary written -- this is the on-demand counterpart,
# for the cases that trigger doesn't cover: backfilling a product whose
# videos were already summarized before this endpoint existed, or
# re-running it after a manual reassign/delete cleanup changed which
# videos count as "approved" for a product without anything re-
# summarizing.
#
# fetch_approved_video_summaries / build_rollup_prompt / generate_video_
# reviews_rollup / store_rollup below are deliberate duplicates of
# video_summarizer/app.py's functions of the same name, NOT imports --
# admin_api and video_summarizer are separate Lambda deployment packages
# (separate CodeUri, no shared module path between them), same "own the
# whole package" convention as scripts/home_transcript_fetcher.py
# duplicating video_transcript_fetcher/app.py's YouTube-fetching logic
# (see that script's module docstring for the fuller reasoning). Keep
# both copies in sync if the prompt or Bedrock wire format ever changes.
# ---------------------------------------------------------------------

DEFAULT_BEDROCK_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_ROLLUP_MAX_TOKENS = 350


def fetch_approved_video_summaries(conn, product_id: str) -> list:
    with conn.cursor() as cur:
        cur.execute(
            """
            select summary from product_videos
            where product_id = %s and status = 'approved' and summary is not null
            order by created_at asc, id asc
            """,
            (product_id,),
        )
        return [row[0] for row in cur.fetchall()]


def build_rollup_prompt(product_name: str, brand_name: str, summaries: list, description: str = None) -> str:
    """Kept in sync with video_summarizer/app.py's function of the same
    name -- see this file's module comment above fetch_approved_video_
    summaries for why these are deliberate duplicates, not a shared import.

    description (optional): the manufacturer's own marketing copy for this
    ball, scraped from its product page (products.description). Included
    as grounding context when present -- useful for getting technical
    details right (core/coverstock names, the lane conditions it's
    marketed for) -- but the prompt is explicit that the output must still
    reflect what reviewers actually said, not just restate marketing copy."""
    context_block = ""
    if description:
        context_block = (
            "\n\nFor context, here is the manufacturer's own description of "
            "this ball. Use it to get technical details right (core/"
            "coverstock names, the lane conditions it's marketed for), but "
            "the summary must still reflect what reviewers actually said, "
            "not just restate marketing copy:\n" + description
        )

    if len(summaries) == 1:
        return (
            f"The following is a summary of a single YouTube review video for the "
            f"{brand_name} {product_name} bowling ball. Rewrite it as a standalone "
            "2-4 sentence product description of what reviewers say about this ball "
            "-- remove any references to \"this video\" or \"the reviewer\", state it "
            "as plain fact about the ball's performance instead."
            f"{context_block}"
            f"\n\nReview summary:\n{summaries[0]}"
        )

    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(summaries, start=1))
    return (
        f"The following are {len(summaries)} independent review summaries for the "
        f"{brand_name} {product_name} bowling ball, each from a different YouTube "
        "review video. Synthesize them into a single 3-5 sentence overview of what "
        "reviewers generally say about this ball -- note common themes (hook shape, "
        "reaction on the lane, who it's recommended for) and call out any notable "
        "disagreements between reviewers rather than papering over them. Don't "
        "reference \"the videos\" or how many reviews there are; write it as a "
        "standalone product description."
        f"{context_block}"
        f"\n\nReview summaries:\n{numbered}"
    )


def generate_video_reviews_rollup(bedrock_client, model_id: str, product_name: str, brand_name: str,
                                   summaries: list, description: str = None,
                                   max_tokens: int = DEFAULT_ROLLUP_MAX_TOKENS) -> str:
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [
            {"role": "user", "content": build_rollup_prompt(product_name, brand_name, summaries, description)},
        ],
    })
    response = bedrock_client.invoke_model(modelId=model_id, contentType="application/json",
                                            accept="application/json", body=body)
    payload = json.loads(response["body"].read())
    return payload["content"][0]["text"].strip()


def store_rollup(conn, product_id: str, rollup_text: str, video_count: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update products
            set video_reviews_summary = %s,
                video_reviews_summary_video_count = %s,
                video_reviews_summary_updated_at = now()
            where id = %s
            """,
            (rollup_text, video_count, product_id),
        )
    conn.commit()


def _fetch_product_for_rollup(conn, product_id: str):
    with conn.cursor() as cur:
        cur.execute(
            "select p.id, p.name, b.name as brand_name, p.description from products p "
            "join brands b on b.id = p.brand_id where p.id = %s",
            (product_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"id": row[0], "name": row[1], "brand_name": row[2], "description": row[3]}


def refresh_video_reviews_rollup(conn, product_id: str) -> dict:
    """Builds its own Bedrock client and reads BEDROCK_MODEL_ID itself
    (same env var video_summarizer uses) rather than taking either as a
    parameter, so app.py's endpoint stays a thin routing call with nothing
    to construct -- consistent with every other service.py function here
    (e.g. _publish_transcript_result_message building its own boto3 SQS
    client). 'no approved+summarized videos yet' is a normal, expected
    outcome (rollup_regenerated: False), not an error -- same convention
    as video_summarizer's own version of this function."""
    product = _fetch_product_for_rollup(conn, product_id)
    if product is None:
        raise LookupError(f"No products row with id {product_id}")

    summaries = fetch_approved_video_summaries(conn, product_id)
    if not summaries:
        return {"product_id": product_id, "rollup_regenerated": False, "reason": "no_summaries"}

    import boto3

    bedrock_client = boto3.client("bedrock-runtime")
    model_id = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL_ID)
    rollup_text = generate_video_reviews_rollup(
        bedrock_client, model_id, product["name"], product["brand_name"], summaries,
        product["description"],
    )
    store_rollup(conn, product_id, rollup_text, len(summaries))
    return {"product_id": product_id, "rollup_regenerated": True, "video_count": len(summaries)}
