"""
SQS-triggered: consumes an approved product_videos row (see
src/admin_api/service.py's approve_video_candidate, which is what publishes
here -- an admin approving a candidate is the only way a message reaches
this queue) and fetches its transcript + a Bedrock-generated summary.

REVISED this deploy after a real live-smoke-test finding: the original
design fetched the full watch page (https://www.youtube.com/watch?v=<id>)
and regex-extracted its embedded `"captionTracks":[...]` JSON blob. That
worked once against a manual curl from a residential IP (confirmed real
data), but the SAME URL fetched from inside this Lambda came back with a
same-size, genuinely-real (not a consent wall -- checked) watch page that
simply didn't have the blob inlined. Repeated identical requests to the
same watch page during testing most likely triggered some page-variant
rotation or soft anti-automation behavior server-side -- not something a
parsing fix addresses, since the content itself was inconsistent between
fetches of the identical URL.

Replaced with YouTube's dedicated, lightweight caption-list endpoint
instead of parsing the full watch page at all:

1. `https://www.youtube.com/api/timedtext?type=list&v=<id>` returns a
   small XML document listing available caption tracks directly --
   `<transcript_list><track lang_code="en" kind="asr" .../>...</transcript_list>`
   (kind="asr" for YouTube's auto-generated captions, absent for
   human-uploaded ones). No watch-page fetch, no JS-blob parsing, no
   dependence on which variant of the watch page YouTube happens to serve
   a given request.
2. Fetching `https://www.youtube.com/api/timedtext?v=<id>&lang=<lang_code>`
   (plus `&kind=asr` for an auto-generated track) returns the same
   `<transcript><text start="..." dur="...">...</text>...</transcript>`
   XML shape the original design already expected -- parse_transcript_xml
   is unchanged.

This is a long-standing, widely-documented (if unofficial) YouTube
endpoint, older than watch-page JSON scraping, and doesn't require the
official OAuth-gated captions.download endpoint. Still genuinely
unverified against a live call this session (same zero-outbound-network
sandbox constraint noted throughout this project) -- confirm the real
shape via a manual curl before fully trusting this, same discipline
applied to every other new parser here.

A short retry (see list_caption_tracks) is layered on top as a cheap
hedge against the same kind of per-request inconsistency that broke the
watch-page approach, since there's no way to rule out this endpoint
having similar variance without more live data than this session has.

Videos with captions disabled entirely have no tracks in the list -- this
is treated as an expected, non-retryable-by-SQS outcome (transcript_note
is set, the message is NOT sent to the DLQ for this), not a bug, since not
every video has captions.

Bedrock (not the Anthropic API directly) per explicit user choice --
reuses this project's existing AWS account/IAM rather than a new external
API key, same reasoning as this project's other AWS-native choices (Secrets
Manager for credentials, SQS for queues).
"""
import json
import logging
import os
import time
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

TIMEDTEXT_BASE_URL = "https://www.youtube.com/api/timedtext"
DEFAULT_LIST_FETCH_ATTEMPTS = 2
DEFAULT_LIST_RETRY_DELAY_SECONDS = 2


def fetch_caption_track_list_xml(video_id: str, timeout: int = 30) -> str:
    """Kept separate from parsing so tests can feed real fixture XML
    without a network call. See module docstring point 1."""
    import requests

    resp = requests.get(
        TIMEDTEXT_BASE_URL,
        params={"type": "list", "v": video_id},
        headers={"User-Agent": "Mozilla/5.0 (compatible; bowling-scraper/1.0)"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.text


def parse_caption_track_list(xml_text: str) -> list:
    """Parses the <track .../> elements out of the type=list response into
    [{"lang_code": "en", "name": "", "kind": "asr"|None}, ...]. Returns []
    for a video with no captions at all (a real, empty-but-valid
    <transcript_list/> response) or for XML that doesn't parse -- both
    treated as "no tracks", not a crash."""
    if not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("Caption track list XML did not parse -- treating as no tracks")
        return []
    return [
        {
            "lang_code": node.get("lang_code"),
            "name": node.get("name", ""),
            "kind": node.get("kind"),  # "asr" or None (human-uploaded)
        }
        for node in root.iter("track")
    ]


def pick_caption_track(tracks: list) -> dict:
    """Prefers a real English human-uploaded track, falling back to
    English auto-generated (kind == 'asr'), falling back to whatever's
    first if no English track exists at all (better than nothing for a
    summary -- Bedrock can still work from non-English text)."""
    if not tracks:
        return None

    def is_english(t):
        return (t.get("lang_code") or "").lower().startswith("en")

    english_human = [t for t in tracks if is_english(t) and t.get("kind") != "asr"]
    if english_human:
        return english_human[0]

    english_any = [t for t in tracks if is_english(t)]
    if english_any:
        return english_any[0]

    return tracks[0]


def list_caption_tracks(video_id: str, max_attempts: int = DEFAULT_LIST_FETCH_ATTEMPTS,
                         delay_seconds: float = DEFAULT_LIST_RETRY_DELAY_SECONDS) -> list:
    """Fetches+parses the caption track list, retrying up to max_attempts
    times if the first attempt comes back empty -- a cheap hedge against
    the same kind of per-request inconsistency that broke the original
    watch-page-scraping approach (see module docstring), since there's no
    live evidence yet ruling out this endpoint having similar variance.
    Does NOT retry on a network error/non-2xx response -- those should
    propagate and let SQS's normal retry/DLQ handling deal with genuine
    transient failures, rather than silently retrying inside the function
    on top of SQS's own retries."""
    for attempt in range(1, max_attempts + 1):
        xml_text = fetch_caption_track_list_xml(video_id)
        tracks = parse_caption_track_list(xml_text)
        if tracks:
            if attempt > 1:
                logger.info("video_id=%s: got %d track(s) on retry attempt %d", video_id, len(tracks), attempt)
            return tracks
        if attempt < max_attempts:
            logger.info(
                "video_id=%s: attempt %d/%d came back with 0 tracks, retrying in %ss",
                video_id, attempt, max_attempts, delay_seconds,
            )
            time.sleep(delay_seconds)
    return []


def build_transcript_url(video_id: str, track: dict) -> tuple:
    """Returns (url, params) for fetching a specific track's transcript --
    kept as a separate pure function so the URL-building logic (notably,
    only including kind=asr for auto-generated tracks) is testable without
    a network call."""
    params = {"v": video_id, "lang": track.get("lang_code") or ""}
    if track.get("name"):
        params["name"] = track["name"]
    if track.get("kind") == "asr":
        params["kind"] = "asr"
    return TIMEDTEXT_BASE_URL, params


def fetch_transcript_xml(url: str, params: dict, timeout: int = 30) -> str:
    import requests

    resp = requests.get(url, params=params, timeout=timeout)
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


def get_transcript(video_id: str) -> tuple:
    """Returns (transcript_text, note). note is None on success, or a short
    explanation (e.g. "no_captions_available") when transcript_text is
    empty -- callers must treat an empty transcript as an expected
    non-error outcome, not raise."""
    tracks = list_caption_tracks(video_id)
    logger.info("Found %d caption track(s) for %s", len(tracks), video_id)

    if not tracks:
        return "", "no_captions_available"

    track = pick_caption_track(tracks)
    url, params = build_transcript_url(video_id, track)
    xml_text = fetch_transcript_xml(url, params)
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
