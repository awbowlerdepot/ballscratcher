"""
SQS-triggered: consumes an approved product_videos row (see
src/admin_api/service.py's approve_video_candidate, which is what publishes
here -- an admin approving a candidate is the only way a message reaches
this queue) and fetches its transcript + a Bedrock-generated summary.

Two real, disclosed things this module's design depends on that could NOT
be verified against a live YouTube page this session (sandbox has zero
outbound network access -- same hard constraint noted throughout this
project; every prior real-data check required the user to run curl
themselves). Both are widely documented, long-standing (if unofficial)
YouTube behaviors, not a guess made up for this session, but they must be
confirmed via a real fetch by the user before this is considered done, the
same discipline this project has applied to every other new parser (e.g.
the commercebuild archived-page template turned out to be stale after a
site redesign -- assumed-but-unverified structure has burned this project
before):

1. A public video's watch page (https://www.youtube.com/watch?v=<id>) embeds
   a `"captionTracks":[...]` JSON array (inside the page's
   ytInitialPlayerResponse blob) listing each available caption track's
   `baseUrl`, `languageCode`, and `kind` ("asr" for YouTube's own
   auto-generated captions, absent for human-uploaded ones). This does NOT
   require the official (OAuth-gated) captions.download endpoint -- it's
   the same public data the watch page's own CC button reads.
2. Fetching a track's baseUrl returns an XML document
   (`<transcript><text start="..." dur="...">...</text>...</transcript>`)
   with one <text> element per caption line. Concatenating those in order
   is the transcript.

Videos with captions disabled entirely have no captionTracks array -- this
is treated as an expected, non-retryable outcome (transcript_note is set,
the SQS message is NOT sent to the DLQ for this), not a bug, since not
every video has captions.

Bedrock (not the Anthropic API directly) per explicit user choice --
reuses this project's existing AWS account/IAM rather than a new external
API key, same reasoning as this project's other AWS-native choices (Secrets
Manager for credentials, SQS for queues).
"""
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from html import unescape

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

_CAPTION_TRACKS_RE = re.compile(r'"captionTracks":(\[.*?\])', re.DOTALL)


def fetch_watch_page(video_id: str, timeout: int = 30) -> str:
    """Kept separate from parsing so tests can feed real fixture HTML
    without a network call. See module docstring point 1 for what this
    page is expected to contain and why it's unverified live this
    session."""
    import requests

    resp = requests.get(
        f"https://www.youtube.com/watch?v={video_id}",
        headers={"User-Agent": "Mozilla/5.0 (compatible; bowling-scraper/1.0)"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.text


def parse_caption_tracks(watch_page_html: str) -> list:
    """Extracts the captionTracks JSON array embedded in the watch page
    (see module docstring point 1). Returns [] if the video has no
    captions at all -- a real, expected outcome, not a parse failure.

    Uses re.DOTALL: found via this module's own test (a fixture with the
    JSON blob spread across multiple lines) that a plain `.` doesn't match
    newlines by default, which would silently return [] against any watch
    page where the surrounding markup happens to wrap that blob across
    lines -- minified real pages likely don't, but there's no reason to
    depend on that."""
    match = _CAPTION_TRACKS_RE.search(watch_page_html)
    if not match:
        return []
    try:
        return json.loads(match.group(1))
    except (ValueError, TypeError):
        logger.warning("captionTracks blob found but not valid JSON -- treating as no captions")
        return []


def pick_caption_track(tracks: list) -> dict:
    """Prefers a real English human-uploaded track, falling back to
    English auto-generated (kind == 'asr'), falling back to whatever's
    first if no English track exists at all (better than nothing for a
    summary -- Bedrock can still work from non-English text)."""
    if not tracks:
        return None

    def is_english(t):
        return (t.get("languageCode") or "").lower().startswith("en")

    english_human = [t for t in tracks if is_english(t) and t.get("kind") != "asr"]
    if english_human:
        return english_human[0]

    english_any = [t for t in tracks if is_english(t)]
    if english_any:
        return english_any[0]

    return tracks[0]


def fetch_transcript_xml(base_url: str, timeout: int = 30) -> str:
    import requests

    resp = requests.get(base_url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_transcript_xml(xml_text: str) -> str:
    """Concatenates every <text> element's content, in document order, into
    a single plain-text transcript (see module docstring point 2). Caption
    text is HTML-entity-escaped in the XML (e.g. &#39; for an apostrophe),
    hence the unescape() call."""
    if not xml_text.strip():
        return ""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("Transcript XML did not parse -- treating as unavailable")
        return ""
    lines = [unescape(node.text) for node in root.iter("text") if node.text]
    return " ".join(line.strip() for line in lines if line.strip())


# Real gap found via this deploy's first live smoke test: a genuinely
# captioned video (confirmed via curl from a residential IP, which returned
# a real captionTracks blob) still came back "no_captions_available" when
# fetched from inside Lambda -- suspected cause is YouTube serving
# different content to a datacenter/NAT-gateway IP (a consent wall or
# stripped-down page) than to a residential one, even with an identical
# User-Agent. These are the markers a YouTube consent-wall page is known to
# contain; checked only to make the next real CloudWatch log line
# diagnostic rather than to change behavior -- a match here doesn't change
# the return value, it just explains WHY tracks came back empty.
_CONSENT_WALL_MARKERS = ("consent.youtube.com", "Before you continue to YouTube")


def get_transcript(video_id: str) -> tuple:
    """Returns (transcript_text, note). note is None on success, or a short
    explanation (e.g. "no_captions_available") when transcript_text is
    empty -- callers must treat an empty transcript as an expected
    non-error outcome, not raise."""
    html = fetch_watch_page(video_id)
    logger.info("Fetched watch page for %s: %d chars", video_id, len(html))

    tracks = parse_caption_tracks(html)
    logger.info("Found %d caption track(s) for %s", len(tracks), video_id)

    if not tracks:
        hit_marker = next((m for m in _CONSENT_WALL_MARKERS if m in html), None)
        if hit_marker:
            logger.warning(
                "video_id=%s: page looks like a consent wall (matched %r), not the real watch page -- "
                "this is very likely why no captionTracks were found, not a genuinely caption-less video",
                video_id, hit_marker,
            )
        else:
            logger.info(
                "video_id=%s: no consent-wall marker found either -- page is %d chars, "
                "first 200: %r", video_id, len(html), html[:200],
            )
        return "", "no_captions_available"

    track = pick_caption_track(tracks)
    xml_text = fetch_transcript_xml(track["baseUrl"])
    transcript = parse_transcript_xml(xml_text)
    if not transcript:
        return "", "captions_listed_but_transcript_fetch_returned_empty"
    return transcript, None


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
    product_video_id = job["product_video_id"]
    conn = get_db_connection()
    try:
        row = fetch_video_row(conn, product_video_id)
        if row is None:
            logger.warning("No product_videos row with id=%s -- skipping", product_video_id)
            return {"product_video_id": product_video_id, "skipped": "not_found"}
        if row["status"] != "approved":
            # Only approved candidates should ever reach this queue (see
            # module docstring) -- if one didn't, treat it as non-retryable
            # rather than looping forever.
            logger.warning("product_videos id=%s status=%s, not 'approved' -- skipping",
                            product_video_id, row["status"])
            return {"product_video_id": product_video_id, "skipped": f"status_is_{row['status']}"}

        transcript, note = get_transcript(row["youtube_video_id"])

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
