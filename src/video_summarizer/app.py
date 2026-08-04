"""
SQS-triggered: consumes a transcript-fetch result (published by
src/video_transcript_fetcher/app.py to VideoTranscriptResultQueue) and
writes it to the DB along with a Bedrock-generated summary, if a
transcript came through.

SPLIT OUT OF THIS FILE this deploy: everything that talks to YouTube
(watch-page fetching/parsing, caption-track selection, transcript XML
parsing, and the retry loop around all of it) now lives in
src/video_transcript_fetcher/app.py, running as its own, separate,
NON-VPC-attached Lambda. Real reasoning, not a style preference:

Two rounds of live smoke-testing (see video_transcript_fetcher's module
docstring for the full history -- watch-page scraping, then
timedtext?type=list, then back to watch-page scraping with retries) found
that `www.youtube.com` (the watch page, and apparently the legacy
timedtext endpoints too) consistently failed to return caption data when
fetched from THIS function's VPC-attached Lambda -- routed through this
account's single, static, heavily-reused NAT gateway IP -- across two
different videos and multiple retry attempts each, while the identical
URL fetched from a residential IP worked. Meanwhile `googleapis.com/
youtube/v3/search` (video_discovery's official, keyed API) works fine from
that same NAT gateway IP. That pattern points at IP-based treatment tied
to this account's specific NAT gateway address, not per-request
randomness -- which a retry loop inside this same network path can't fix.

This function still needs DB access (VPC-attached, DbSecretArn) and
Bedrock access, so it stays as-is on the network side. The YouTube-facing
work moved to a function that deliberately has NEITHER of those needs, so
it can run without VPC attachment and use AWS's normal, shared execution
network path instead of this account's fixed NAT gateway EIP -- a
genuinely different, ordinary AWS network path, not an attempt to disguise
traffic as residential (no rotating proxies, no fingerprint spoofing --
same "don't build anti-bot-defense workarounds" line this project has held
for Instagram/TikTok/etc. throughout). Unverified whether it actually
resolves the caption-fetching issue as of this deploy -- confirm via a
real invoke, same real-data-first discipline as everything else in this
project.

Bedrock (not the Anthropic API directly) per explicit user choice --
reuses this project's existing AWS account/IAM rather than a new external
API key, same reasoning as this project's other AWS-native choices (Secrets
Manager for credentials, SQS for queues).

"SUMMARY OF SUMMARIES": as of this deploy, every time a video gets a real
summary written (see _process_one), this function also regenerates
products.video_reviews_summary -- one product-level rollup synthesized
from every approved video's current summary for that product (see
refresh_video_reviews_rollup / fetch_approved_video_summaries /
build_rollup_prompt / 006_products_video_reviews_summary.sql). Explicit
product decisions this was built against: regeneration is automatic (no
separate trigger/endpoint), a single summarized video is enough to
produce a rollup (not gated behind a minimum count), and -- the one open
question when this was speced -- what a lone video's rollup should look
like: rather than just copying that one product_videos.summary verbatim
into video_reviews_summary, build_rollup_prompt still runs it through
Bedrock with a different prompt (rewrite-as-standalone vs synthesize-
multiple). Reasoning: a per-video summary is written in that video's
context ("in this video, the reviewer notes...") and copying that framing
verbatim into a field meant to read as a standalone product description
would mean the field's voice shifts the moment a second review comes in.
One extra cheap Bedrock call buys a consistent voice regardless of video
count. This step is soft-fail (wrapped in its own try/except in
_process_one) -- it's a derived convenience field, not the source of
truth (each product_videos row's own summary still is that), so a Bedrock
hiccup here must not turn an otherwise-successful video summarization
into an SQS retry/DLQ.
"""
import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Global cross-Region inference profile ID, not a bare on-demand model id --
# confirmed via Bedrock's own model-card page that Claude Haiku 4.5 has no
# in-Region support in us-west-1 (this stack's region), only Geographic/
# Global cross-Region inference. Matches template.yaml's BedrockModelId
# default -- this constant only matters as a fallback for a manual/local
# invoke that skips the Lambda environment entirely, since BEDROCK_MODEL_ID
# is always set by the deployed function. See template.yaml's
# BedrockModelId/BedrockBaseModelId parameter descriptions for the full
# reasoning and the IAM policy shape this requires.
DEFAULT_BEDROCK_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_TRANSCRIPT_CHAR_LIMIT = 12000
DEFAULT_SUMMARY_MAX_TOKENS = 300
DEFAULT_ROLLUP_MAX_TOKENS = 350


def build_summary_prompt(product_name: str, brand_name: str, video_title: str, transcript: str) -> str:
    truncated = transcript[:DEFAULT_TRANSCRIPT_CHAR_LIMIT]
    return (
        f"The following is a transcript of a YouTube video titled {video_title!r}, "
        f"believed to be a review or reaction video for the {brand_name} {product_name} "
        "bowling ball. Write a concise 2-4 sentence summary covering only what the "
        "speaker says about this specific ball (hook shape, reaction on the lane, "
        "coverstock/core impressions, who it's recommended for). If the transcript "
        "isn't actually about this ball, say so plainly instead of guessing.\n\n"
        f"Transcript:\n{truncated}"
    )


def summarize_transcript(bedrock_client, model_id: str, product_name: str, brand_name: str,
                          video_title: str, transcript: str,
                          max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS) -> str:
    """Calls Bedrock's InvokeModel with an Anthropic-model-shaped request
    body (anthropic_version + messages, the standard Bedrock wire format for
    Claude models -- see AWS Bedrock docs). Kept separate from the boto3
    client construction so tests can pass a fake client with a canned
    response instead of hitting real AWS."""
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [
            {"role": "user", "content": build_summary_prompt(product_name, brand_name, video_title, transcript)},
        ],
    })
    response = bedrock_client.invoke_model(modelId=model_id, contentType="application/json",
                                            accept="application/json", body=body)
    payload = json.loads(response["body"].read())
    return payload["content"][0]["text"].strip()


def build_rollup_prompt(product_name: str, brand_name: str, summaries: list, description: str = None) -> str:
    """Two different prompts depending on how many review summaries feed
    in -- not because the underlying task differs (both are "describe what
    reviewers say about this ball"), but a single per-video summary is
    written in that one video's context ("in this video, the reviewer
    notes...") and copying it verbatim into a product-level field would
    carry that framing along. Rewriting even a single source keeps the
    field's voice consistent regardless of video count -- see module
    docstring's SUMMARY OF SUMMARIES section for the full reasoning.

    description (optional): the manufacturer's own marketing copy for this
    ball, scraped from its product page (products.description -- see
    product_scraper/commercebuild_product_scraper/woocommerce_product_
    scraper/netsuite_product_scraper's parse_description functions, added
    once real product pages across all four platforms were confirmed to
    carry ball-specific description text, not just generic tier/tech
    blurbs). Included as grounding context when present -- useful for
    getting technical details right (core/coverstock names, the lane
    conditions the manufacturer markets it for) -- but the prompt is
    explicit that the output must still reflect what reviewers actually
    said, not just restate marketing copy. Optional because not every
    product has one yet (rows written before this field existed, or a
    genuine parse miss); the rollup already worked fine without it, this
    only adds context when available."""
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
    """Same Bedrock wire format as summarize_transcript -- kept as a
    separate function (not summarize_transcript with a different prompt
    argument) since the two have genuinely different inputs (a transcript
    vs a list of existing summaries) and callers."""
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


def fetch_video_row(conn, product_video_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            select pv.id, pv.youtube_video_id, pv.title, pv.status, pv.product_id,
                   p.name as product_name, b.name as brand_name, p.description as product_description
            from product_videos pv
            join products p on p.id = pv.product_id
            join brands b on b.id = p.brand_id
            where pv.id = %s
            """,
            (product_video_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cur.description]
        return dict(zip(columns, row))


def store_result(conn, product_video_id: str, transcript: str, transcript_note: str, summary: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update product_videos
            set transcript = %s, transcript_note = %s, summary = %s
            where id = %s
            """,
            (transcript or None, transcript_note, summary, product_video_id),
        )
    conn.commit()


def fetch_approved_video_summaries(conn, product_id: str) -> list:
    """Every non-null summary belonging to this product's currently
    approved videos, oldest-created first (a stable, deterministic order
    so the rollup prompt -- and therefore its numbered list for the
    multi-summary case -- doesn't reshuffle between regenerations for no
    reason). A video that's been rejected is excluded even if it happens
    to have a leftover summary from before rejection: the rollup should
    reflect 'what the currently-approved videos say', not a slice of the
    table's full history. id is a final tiebreaker for the same reason
    list_video_candidates needed one (see admin_api/service.py) -- rows
    inserted in the same batch can share a created_at timestamp."""
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


def refresh_video_reviews_rollup(conn, bedrock_client, model_id: str, product_id: str,
                                  product_name: str, brand_name: str, description: str = None) -> dict:
    """Regenerates products.video_reviews_summary from every approved
    video's current summary for this product -- called from _process_one
    after a video just got a real summary written (see module docstring's
    SUMMARY OF SUMMARIES section for why this runs on every summarization,
    not behind a minimum-count gate: a single summarized video still
    produces a rollup, via build_rollup_prompt's single-source branch).
    Deliberately does not special-case 'went from N summaries to the same
    N' (e.g. a video got re-summarized with identical content) --
    regenerating is cheap and always correct, so there's no real benefit to
    detecting and skipping a no-op case.

    description: the product's manufacturer-page description, if any --
    see build_rollup_prompt's docstring. Passed through as-is (may be
    None); the caller (_process_one) already has it from fetch_video_row's
    join, so this function doesn't need its own query for it."""
    summaries = fetch_approved_video_summaries(conn, product_id)
    if not summaries:
        # Shouldn't happen in the caller's actual flow (this only runs
        # right after storing a new summary for this exact product), but
        # kept as a real guard rather than assumed -- fetch_approved_video_
        # summaries' filters (status='approved', summary is not null) are
        # independent of what _process_one just wrote, so a race (the video
        # got rejected between store_result and here) is possible, if rare.
        return {"product_id": product_id, "rollup_regenerated": False, "reason": "no_summaries"}

    rollup_text = generate_video_reviews_rollup(bedrock_client, model_id, product_name, brand_name,
                                                 summaries, description)
    store_rollup(conn, product_id, rollup_text, len(summaries))
    return {"product_id": product_id, "rollup_regenerated": True, "video_count": len(summaries)}


def _process_one(job: dict, bedrock_client) -> dict:
    """job now comes from video_transcript_fetcher via
    VideoTranscriptResultQueue and already carries the fetch outcome
    (transcript text, possibly empty, plus a note) -- this function no
    longer talks to YouTube itself, only DB + Bedrock."""
    product_video_id = job["product_video_id"]
    transcript = job.get("transcript") or ""
    note = job.get("transcript_note")

    conn = get_db_connection()
    try:
        row = fetch_video_row(conn, product_video_id)
        if row is None:
            logger.warning("No product_videos row with id=%s -- skipping", product_video_id)
            return {"product_video_id": product_video_id, "skipped": "not_found"}
        if row["status"] != "approved":
            # Only approved candidates should ever reach this pipeline (see
            # module docstring) -- if one didn't, treat it as non-retryable
            # rather than looping forever.
            logger.warning("product_videos id=%s status=%s, not 'approved' -- skipping",
                            product_video_id, row["status"])
            return {"product_video_id": product_video_id, "skipped": f"status_is_{row['status']}"}

        summary = None
        if transcript:
            model_id = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL_ID)
            summary = summarize_transcript(
                bedrock_client, model_id, row["product_name"], row["brand_name"],
                row["title"], transcript,
            )

        store_result(conn, product_video_id, transcript, note, summary)

        rollup_regenerated = False
        if summary is not None:
            # Soft-fail by design -- see module docstring's SUMMARY OF
            # SUMMARIES section. This video's own transcript+summary is
            # already committed above regardless of what happens here.
            try:
                rollup_result = refresh_video_reviews_rollup(
                    conn, bedrock_client, os.environ.get("BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL_ID),
                    row["product_id"], row["product_name"], row["brand_name"], row["product_description"],
                )
                rollup_regenerated = rollup_result["rollup_regenerated"]
            except Exception:
                logger.exception(
                    "Failed to refresh video_reviews_summary rollup for product_id=%s -- "
                    "this video's own summary was still saved successfully, leaving any "
                    "existing rollup untouched",
                    row["product_id"],
                )

        return {
            "product_video_id": product_video_id,
            "transcript_chars": len(transcript),
            "transcript_note": note,
            "summarized": summary is not None,
            "rollup_regenerated": rollup_regenerated,
        }
    finally:
        conn.close()


def _extract_jobs(event: dict) -> list:
    if "Records" in event:
        return [(json.loads(r["body"]), r["messageId"]) for r in event["Records"]]
    return [(event, None)]


def handler(event, context):
    jobs = _extract_jobs(event)

    import boto3
    bedrock_client = boto3.client("bedrock-runtime")

    results = []
    batch_item_failures = []
    for job, message_id in jobs:
        try:
            results.append(_process_one(job, bedrock_client))
        except Exception:
            logger.exception("Failed to summarize video job: %r", job)
            if message_id is not None:
                batch_item_failures.append({"itemIdentifier": message_id})
            else:
                raise

    response = {"statusCode": 200, "body": json.dumps({"results": results})}
    if "Records" in event:
        response["batchItemFailures"] = batch_item_failures
    return response
