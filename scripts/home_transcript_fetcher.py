#!/usr/bin/env python3
"""
Standalone script, meant to run on hardware you control at home (a
Raspberry Pi, a spare box, whatever's on your own residential connection)
-- NOT deployed to AWS, NOT part of the SAM stack. Run it on a daily cron
job and it'll pick up any approved video candidate still missing a
transcript, fetch it from YouTube using your home connection, and hand the
result off to the admin API.

WHY THIS EXISTS, briefly (full evidence trail in
src/video_transcript_fetcher/app.py's module docstring): this session
found, via repeated real live tests, that YouTube's watch-page caption
data comes back empty from every AWS Lambda network path tried --
VPC-attached (this account's NAT gateway) and non-VPC (AWS's shared
execution pool) both failed identically, across multiple videos, even with
a real browser User-Agent. The same fetch succeeds from a residential
connection. That's consistent with IP/ASN-reputation-based detection
(cloud provider IP ranges get treated differently, independent of which
specific IP or which headers), which no code change inside AWS can fix.

This script is the honest way around that: it's not a rotating-proxy pool
built to disguise bulk automated traffic as residential (the technique
this project has explicitly ruled out for this feature and everything
else scraped in this codebase) -- it's a low-volume script, running once a
day, from a real residential connection you actually control, doing
exactly what you were already doing by hand with curl earlier this
session, just automated for convenience. Still worth being honest that
YouTube's Terms of Service (Section 5.B) prohibit "any automated means"
full stop, regardless of whose IP it's on -- this doesn't make the fetch
fully ToS-compliant, it just moves it out of the actively-detected,
disguised-traffic category into something closer to what an individual
human checking a few videos a day would do by hand.

Deliberately duplicates (rather than imports) the caption-fetching logic
from src/video_transcript_fetcher/app.py: that module lives inside the
Lambda deployment package (AWS-specific requirements, expects to run
inside the SAM-built src/ tree) and this script is meant to be copied to
a Pi/home server on its own, with nothing installed beyond `requests`. If
YouTube's page markup changes and parse_caption_tracks/parse_transcript_xml
need fixing, fix BOTH copies -- see that module's version for the
authoritative one; this one mirrors it.

Setup on the Pi/home server:
    pip install requests
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used for other admin API calls>"
    python3 home_transcript_fetcher.py

Cron, once a day (adjust the path):
    0 7 * * * ADMIN_API_URL=... ADMIN_API_TOKEN=... /usr/bin/python3 /home/pi/home_transcript_fetcher.py >> /var/log/bowling-transcript-fetcher.log 2>&1

Or, to avoid putting the token in your crontab in plaintext, put the
env vars in a small shell wrapper (chmod 600) and call that from cron
instead. See DEPLOY_RUNBOOK.md's "Home transcript fetcher" section for
the full setup walkthrough.
"""
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from html import unescape

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("home_transcript_fetcher")

DEFAULT_WATCH_PAGE_FETCH_ATTEMPTS = 3
DEFAULT_WATCH_PAGE_RETRY_DELAY_SECONDS = 2
DEFAULT_DELAY_BETWEEN_VIDEOS_SECONDS = 3  # be a polite, low-volume, once-a-day caller, not a scraper hammering the site
DEFAULT_PAGE_LIMIT = 200

_CAPTION_TRACKS_RE = re.compile(r'"captionTracks":(\[.*?\])', re.DOTALL)
_CONSENT_WALL_MARKERS = ("consent.youtube.com", "Before you continue to YouTube")


# --- YouTube fetching (mirrors src/video_transcript_fetcher/app.py -- see
# that module's docstring for the full history/reasoning behind this exact
# approach; this is a deliberate duplicate, not a shared import, see this
# file's own module docstring for why) ---

def fetch_watch_page(video_id: str, timeout: int = 30) -> str:
    import requests

    resp = requests.get(
        f"https://www.youtube.com/watch?v={video_id}",
        headers={"User-Agent": "Mozilla/5.0 (compatible; bowling-scraper-home-fetcher/1.0)"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.text


def parse_caption_tracks(watch_page_html: str) -> list:
    match = _CAPTION_TRACKS_RE.search(watch_page_html)
    if not match:
        return []
    try:
        return json.loads(match.group(1))
    except (ValueError, TypeError):
        logger.warning("captionTracks blob found but not valid JSON -- treating as no captions")
        return []


def pick_caption_track(tracks: list) -> dict:
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
    html = ""
    for attempt in range(1, max_attempts + 1):
        html = fetch_watch_page(video_id)
        tracks = parse_caption_tracks(html)
        if tracks:
            if attempt > 1:
                logger.info("video_id=%s: got %d track(s) on retry attempt %d", video_id, len(tracks), attempt)
            return tracks
        if attempt < max_attempts:
            logger.info("video_id=%s: attempt %d/%d came back with 0 tracks, retrying in %ss",
                         video_id, attempt, max_attempts, delay_seconds)
            time.sleep(delay_seconds)

    hit_marker = next((m for m in _CONSENT_WALL_MARKERS if m in html), None)
    if hit_marker:
        logger.warning("video_id=%s: final attempt's page looks like a consent wall (matched %r)",
                        video_id, hit_marker)
    else:
        logger.info("video_id=%s: exhausted %d attempts, no consent-wall marker either -- page is %d chars",
                     video_id, max_attempts, len(html))
    return []


def fetch_transcript_xml(base_url: str, timeout: int = 30) -> str:
    import requests

    resp = requests.get(base_url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_transcript_xml(xml_text: str) -> str:
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
    """Returns (transcript_text, note), same contract as
    video_transcript_fetcher.get_transcript."""
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


# --- Admin API client + orchestration ---

def needs_transcript(candidate: dict) -> bool:
    """A candidate needs work if it's approved, hasn't already got a
    transcript_note recorded (meaning nothing's tried it yet -- either the
    Lambda-based fetcher or a prior run of this script), and doesn't
    already have a summary. Deliberately does NOT retry candidates that
    already have a transcript_note set (even 'no_captions_available') --
    that was a real, checked attempt, not worth re-fetching every single
    day forever. If you want to force a recheck for a specific video
    (e.g. the uploader added captions since), clear its transcript_note
    via psql and it'll be picked up on the next run."""
    return (
        candidate.get("status") == "approved"
        and candidate.get("transcript_note") is None
        and not candidate.get("has_summary")
    )


def list_candidates_needing_transcripts(admin_api_url: str, token: str, page_limit: int = DEFAULT_PAGE_LIMIT) -> list:
    import requests

    headers = {"Authorization": f"Bearer {token}"}
    needing_work = []
    offset = 0
    while True:
        resp = requests.get(
            f"{admin_api_url.rstrip('/')}/video-candidates",
            params={"status": "approved", "limit": page_limit, "offset": offset},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        page = resp.json()["items"]
        needing_work.extend(c for c in page if needs_transcript(c))
        if len(page) < page_limit:
            break
        offset += page_limit
    return needing_work


def submit_transcript(admin_api_url: str, token: str, video_id: str, transcript: str, transcript_note: str) -> None:
    import requests

    resp = requests.post(
        f"{admin_api_url.rstrip('/')}/video-candidates/{video_id}/transcript",
        json={"transcript": transcript, "transcript_note": transcript_note},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()


def run(admin_api_url: str, token: str, delay_between_videos: float = DEFAULT_DELAY_BETWEEN_VIDEOS_SECONDS,
        get_transcript_fn=None) -> dict:
    """get_transcript_fn defaults to this module's own HTTP-based
    get_transcript, but callers can pass a different one -- see
    scripts/home_transcript_fetcher_browser.py, which imports this
    function and passes a Playwright-based fetcher instead. The admin-API
    listing/submission/filtering logic below doesn't care how a transcript
    was obtained, so it's shared rather than duplicated."""
    fetch = get_transcript_fn if get_transcript_fn is not None else get_transcript

    candidates = list_candidates_needing_transcripts(admin_api_url, token)
    logger.info("Found %d approved candidate(s) needing a transcript", len(candidates))

    got_transcript = 0
    no_captions = 0
    errors = 0
    for i, candidate in enumerate(candidates):
        video_id = candidate["id"]
        youtube_video_id = candidate["youtube_video_id"]
        try:
            transcript, note = fetch(youtube_video_id)
            submit_transcript(admin_api_url, token, video_id, transcript, note)
            if transcript:
                got_transcript += 1
            else:
                no_captions += 1
        except Exception:
            # One video's failure shouldn't stop the rest of the batch --
            # same "don't let one bad item block the others" principle as
            # every SQS-consumer handler in this project, just without SQS
            # itself to lean on here.
            logger.exception("Failed processing product_video_id=%s (youtube_video_id=%s)",
                              video_id, youtube_video_id)
            errors += 1

        if i < len(candidates) - 1:
            time.sleep(delay_between_videos)

    summary = {"total": len(candidates), "got_transcript": got_transcript, "no_captions": no_captions, "errors": errors}
    logger.info("Done: %s", summary)
    return summary


def main():
    admin_api_url = os.environ.get("ADMIN_API_URL")
    token = os.environ.get("ADMIN_API_TOKEN")
    if not admin_api_url or not token:
        logger.error("ADMIN_API_URL and ADMIN_API_TOKEN must both be set -- see this script's module docstring for setup.")
        sys.exit(1)

    summary = run(admin_api_url, token)
    # Non-zero exit only if every single candidate errored out (a real
    # systemic failure, e.g. bad token or admin API down) -- individual
    # no-captions-available results are expected, non-error outcomes and
    # shouldn't make a cron job's monitoring flag a red every day.
    if summary["total"] > 0 and summary["errors"] == summary["total"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
