"""
SQS-triggered: consumes an approved product_videos row's job (see
src/admin_api/service.py's approve_video_candidate, which is what publishes
here) and tries to fetch its transcript from YouTube. Publishes the result
(transcript text, or a note explaining why there isn't one) to
VideoTranscriptResultQueue for video_summarizer to pick up and finish the
job (DB write + Bedrock summary).

WHY THIS IS ITS OWN FUNCTION, split out of video_summarizer this deploy:
real, live-tested evidence (not a guess) that YouTube's consumer-facing
surface (www.youtube.com -- the watch page, and apparently also the legacy
timedtext?type=list endpoint) is treated very differently from its
official developer API (googleapis.com/youtube/v3/search, used by
video_discovery, which works fine from the same Lambda/NAT-gateway IP).
Two different videos, fetched from video_summarizer's VPC-attached Lambda
(routed through this account's NAT gateway), both consistently failed to
return caption data across multiple retries -- while the exact same watch
page URL, fetched via a manual curl from a residential IP, DID return real
caption data. That points at IP-based treatment tied to this account's
specific, static, heavily-reused NAT gateway address, not randomness and
not something a parsing fix or a retry loop can solve.

This function is deliberately NOT VPC-attached (no DB access needed here,
which is what made video_summarizer's VPC attachment necessary in the
first place) -- a non-VPC Lambda gets its outbound internet access from
AWS's normal, shared execution environment rather than one fixed,
repeatedly-hit NAT gateway EIP tied to this account. This is a genuinely
different, ordinary AWS network path, not an attempt to disguise traffic
as residential (rotating proxies, browser fingerprint spoofing, etc. were
explicitly ruled out -- same "don't build anti-bot-defense workarounds"
line this project already drew for Instagram/TikTok/etc.). Whether it
actually helps is unverified this session (zero outbound network access in
this sandbox, same constraint as everywhere else in this project) --
confirm via a real invoke before assuming it fixed anything.

There is no official, unauthenticated way to fetch a transcript for a
video you don't own (the real OAuth-gated captions.download API requires
the video owner's consent, useless for third-party review videos), so this
remains best-effort regardless of network path: a video that genuinely has
no captions, or one YouTube blocks regardless of source IP, will still
come back with no transcript -- transcript_note communicates that as an
expected, non-error outcome, not a failure.

1. A public video's watch page embeds a `"captionTracks":[...]` JSON array
   (inside the page's ytInitialPlayerResponse blob) listing each available
   caption track's `baseUrl`, `languageCode`, and `kind` ("asr" for
   YouTube's own auto-generated captions, absent for human-uploaded ones).
2. Fetching a track's baseUrl returns an XML document
   (`<transcript><text start="..." dur="...">...</text>...</transcript>`)
   with one <text> element per caption line. Concatenating those in order
   is the transcript.
"""
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from html import unescape

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_WATCH_PAGE_FETCH_ATTEMPTS = 3
DEFAULT_WATCH_PAGE_RETRY_DELAY_SECONDS = 2

_CAPTION_TRACKS_RE = re.compile(r'"captionTracks":(\[.*?\])', re.DOTALL)

# A real, confirmed consent-wall page is one possible (though, based on
# this deploy's evidence, probably not the main) reason a fetch could come
# back with no captionTracks -- checked only to make the CloudWatch log
# line diagnostic, not to change behavior.
_CONSENT_WALL_MARKERS = ("consent.youtube.com", "Before you continue to YouTube")


def fetch_watch_page(video_id: str, timeout: int = 30) -> str:
    """Kept separate from parsing so tests can feed real fixture HTML
    without a network call. See module docstring point 1.

    TESTED AND REVERTED: briefly sent a real Chrome User-Agent string
    instead of this honest, self-identifying one, as an explicit
    user-requested one-variable diagnostic (does YouTube key off the
    request looking like a real browser?). Live-tested against the same
    two videos used throughout this investigation -- identical
    no_captions_available result, no different from the honest UA. Combined
    with the non-VPC network-path test (also no difference) and a real
    browser's page source confirmed to genuinely contain captionTracks
    (so this isn't a "needs JS execution" problem), the evidence now points
    at IP/ASN-reputation-based detection (this Lambda's egress, VPC or not,
    is still AWS/cloud IP space, unlike a residential connection) rather
    than anything about the request's headers or network path. The only
    remaining lever would be routing through a residential IP, which is
    the proxy/traffic-disguising category this project has held the line
    against from the start -- so this stays reverted to the honest UA."""
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
    captions at all, or if this particular fetch just didn't include the
    blob -- either way, "no tracks this attempt", not a crash.

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


def list_caption_tracks(video_id: str, max_attempts: int = DEFAULT_WATCH_PAGE_FETCH_ATTEMPTS,
                         delay_seconds: float = DEFAULT_WATCH_PAGE_RETRY_DELAY_SECONDS) -> list:
    """Fetches the watch page and extracts captionTracks, retrying up to
    max_attempts times if a fetch comes back empty. Does NOT retry on a
    network error/non-2xx response -- those should propagate and let SQS's
    normal retry/DLQ handling deal with genuine transient failures, rather
    than silently retrying inside the function on top of SQS's own
    retries."""
    html = ""
    for attempt in range(1, max_attempts + 1):
        html = fetch_watch_page(video_id)
        tracks = parse_caption_tracks(html)
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

    hit_marker = next((m for m in _CONSENT_WALL_MARKERS if m in html), None)
    if hit_marker:
        logger.warning(
            "video_id=%s: final attempt's page looks like a consent wall (matched %r)",
            video_id, hit_marker,
        )
    else:
        logger.info(
            "video_id=%s: exhausted %d attempts, no consent-wall marker either -- "
            "page is %d chars, first 200: %r",
            video_id, max_attempts, len(html), html[:200],
        )
    return []


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


def get_transcript(video_id: str) -> tuple:
    """Returns (transcript_text, note). note is None on success, or a short
    explanation (e.g. "no_captions_available") when transcript_text is
    empty -- callers must treat an empty transcript as an expected
    non-error, best-effort outcome, not raise."""
    tracks = list_caption_tracks(video_id)
    logger.info("Found %d caption track(s) for %s", len(tracks), video_id)

    if not tracks:
        return "", "no_captions_available"

    track = pick_caption_track(tracks)
    xml_text = fetch_transcript_xml(track["baseUrl"])
    transcript = parse_transcript_xml(xml_text)
    if not transcript:
        return "", "captions_listed_but_transcript_fetch_returned_empty"
    return transcript, None


# Real safety cap, not seen in practice yet: SQS message bodies are capped
# at 256KB. A transcript alone is very unlikely to approach that (even a
# multi-hour video's captions are plain text, typically well under this),
# but truncating defensively here is cheap insurance against ever failing
# to hand off a result over a pathological edge case -- better a truncated
# transcript makes it through than the handoff message fails to publish at
# all. video_summarizer's own DEFAULT_TRANSCRIPT_CHAR_LIMIT (12000, applied
# before the transcript ever reaches Bedrock) is far smaller than this, so
# this cap should never actually bind in practice.
MAX_TRANSCRIPT_CHARS_FOR_HANDOFF = 100_000


def build_result_message(product_video_id: str, transcript: str, transcript_note: str) -> str:
    return json.dumps({
        "product_video_id": product_video_id,
        "transcript": transcript[:MAX_TRANSCRIPT_CHARS_FOR_HANDOFF],
        "transcript_note": transcript_note,
    })


def _process_one(job: dict, sqs_client) -> dict:
    product_video_id = job["product_video_id"]
    youtube_video_id = job["youtube_video_id"]

    transcript, note = get_transcript(youtube_video_id)

    result_queue_url = os.environ["TRANSCRIPT_RESULT_QUEUE_URL"]
    sqs_client.send_message(
        QueueUrl=result_queue_url,
        MessageBody=build_result_message(product_video_id, transcript, note),
    )

    return {
        "product_video_id": product_video_id,
        "transcript_chars": len(transcript),
        "transcript_note": note,
    }


def _extract_jobs(event: dict) -> list:
    if "Records" in event:
        return [(json.loads(r["body"]), r["messageId"]) for r in event["Records"]]
    return [(event, None)]


def handler(event, context):
    jobs = _extract_jobs(event)

    import boto3
    sqs_client = boto3.client("sqs")

    results = []
    batch_item_failures = []
    for job, message_id in jobs:
        try:
            results.append(_process_one(job, sqs_client))
        except Exception:
            logger.exception("Failed to fetch transcript for job: %r", job)
            if message_id is not None:
                batch_item_failures.append({"itemIdentifier": message_id})
            else:
                raise

    response = {"statusCode": 200, "body": json.dumps({"results": results})}
    if "Records" in event:
        response["batchItemFailures"] = batch_item_failures
    return response
