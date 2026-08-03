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
            select pv.id, pv.youtube_video_id, pv.title, pv.status,
                   p.name as product_name, b.name as brand_name
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
        return {
            "product_video_id": product_video_id,
            "transcript_chars": len(transcript),
            "transcript_note": note,
            "summarized": summary is not None,
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
