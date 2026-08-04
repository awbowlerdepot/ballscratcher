#!/usr/bin/env python3
"""
Browser-automation variant of home_transcript_fetcher.py, built for the
Raspberry Pi 5 -- reuses that script's admin-API plumbing
(list_candidates_needing_transcripts / submit_transcript / needs_transcript
/ run) but replaces the YouTube-fetching step entirely.

WHY THIS EXISTS: home_transcript_fetcher.py's plain-HTTP approach (regex
the watch page for captionTracks, then GET the track's signed baseUrl) got
past the network/IP wall that blocked every AWS Lambda attempt -- caption
track LISTING genuinely works from a residential connection -- but then
hit a different, harder wall at the actual CONTENT fetch: those baseUrls
carry `exp=xpe`, marking them as requiring a PoToken (YouTube's
"proof-of-origin" anti-bot token, part of its BotGuard system). Confirmed
via a real, live test: the signed URL came back as a 200 with a genuinely
empty body, both with and without a shared cookie session, matching a
known, currently-open issue in the youtube-transcript-api project
(jdepoix/youtube-transcript-api#592). Generating a PoToken means solving
that BotGuard challenge computationally -- deliberately NOT done here,
since that's specifically designed to distinguish real browsers from
scripts, and defeating it is bot-detection evasion, the same category this
project has turned down for Instagram/TikTok/etc. throughout.

This script takes a genuinely different approach instead of trying harder
at the same one: YouTube's video page has a real "Show transcript" button
that renders the transcript in a panel using the page's own already-
authenticated JavaScript session (an internal "get_transcript" Innertube
call the page's own code makes, not something this script constructs
itself). A real, unmodified headless Chromium browser (via Playwright)
loading the actual page and clicking that button doesn't solve or forge
anything -- it's an actual browser doing exactly what a browser does, the
same as if a person sat down and read the transcript panel themselves,
just automated for convenience. That's a meaningfully different case from
PoToken generation: it doesn't try to trick YouTube's bot detection, it
simply IS the real thing that detection is checking for. Still worth being
honest, same caveat as home_transcript_fetcher.py's docstring: YouTube's
ToS (Section 5.B) prohibits "any automated means" regardless of mechanism,
so this isn't fully compliant either -- just non-deceptive, low-volume
(once a day), and run from hardware you control.

CONFIRMED WORKING against a real video (DcbP2eltVsE, on the Pi 5) as of
this revision: the "Show transcript" button click succeeds, and the
transcript panel genuinely renders each caption line as a
`<transcript-segment-view-model class="ytwTranscriptSegmentViewModelHost">`
element, with the caption text in a nested
`<span class="ytAttributedStringHost ...">` separate from the timestamp
div -- see _extract_transcript_text's docstring for the real markup this
was confirmed against. The FIRST version of this script guessed at
`ytd-transcript-segment-list-renderer`/`ytd-transcript-segment-renderer`
(the older, "ytd-" prefixed naming convention YouTube's other UI still
uses in places) and that guess was wrong -- caught via this script's own
debug screenshot/HTML dump, not by guessing again. YouTube's DOM and class
names aren't documented and can still drift over time, so this remains a
"confirmed as of one real test," not a permanent guarantee -- this script
still writes a screenshot + full page HTML dump to ./debug/ on any future
failure to find the button or extract text, specifically so real evidence
(not more guessing) can drive whatever selector fixes turn out to be
needed next -- same evidence-first discipline as every other diagnostic in
this project. Send those files back if it stops working after a YouTube
UI change.

Setup on the Pi 5:
    pip install -r scripts/requirements-browser.txt
    playwright install chromium
    playwright install-deps    # pulls in system libraries Chromium needs on a fresh Raspberry Pi OS install
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    python3 scripts/home_transcript_fetcher_browser.py

Cron, once a day -- same pattern as home_transcript_fetcher.py, see
DEPLOY_RUNBOOK.md's Pi 5 setup section:
    0 7 * * * /home/pi/run_browser_transcript_fetcher.sh >> /var/log/bowling-transcript-fetcher-browser.log 2>&1

Set TRANSCRIPT_FETCHER_HEADLESS=false (with a display or VNC session
attached) to watch the browser work while debugging selectors -- defaults
to headless (true) for normal unattended cron operation.
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from home_transcript_fetcher import run as _run_with_admin_api  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("home_transcript_fetcher_browser")

DEFAULT_PAGE_LOAD_TIMEOUT_MS = 30_000
DEFAULT_TRANSCRIPT_BUTTON_TIMEOUT_MS = 8_000
DEFAULT_TRANSCRIPT_PANEL_TIMEOUT_MS = 8_000
DEBUG_DIR = os.path.join(os.path.dirname(__file__), "debug")

# Multiple fallback selector strategies for the "Show transcript" button --
# text-based/role-based first (more resilient to YouTube's frequent DOM/CSS
# changes than a class-name selector would be), a couple of plausible class
# names last as a fallback. Try each in order; take the first that's
# actually visible.
_SHOW_TRANSCRIPT_SELECTORS = [
    'button:has-text("Show transcript")',
    '[aria-label="Show transcript"]',
    'ytd-button-renderer:has-text("Show transcript") button',
]

# Same idea for the "...more" expand button that sometimes hides
# "Show transcript" until the description area is expanded.
_EXPAND_DESCRIPTION_SELECTORS = [
    'tp-yt-paper-button:has-text("...more")',
    '#expand',
    'button:has-text("more")',
]

_NOTE_NO_TRANSCRIPT_BUTTON = "no_captions_available"  # matches home_transcript_fetcher.py's convention
_NOTE_PANEL_FOUND_BUT_EMPTY = "transcript_panel_found_but_text_extraction_returned_empty"


def _dump_debug_evidence(page, video_id: str, tag: str) -> None:
    """Screenshot + full page HTML, written to ./debug/ -- see module
    docstring: don't want to keep guessing about YouTube's DOM shape when
    real evidence is one screenshot away, same discipline as every other
    real diagnostic in this project."""
    os.makedirs(DEBUG_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    base = os.path.join(DEBUG_DIR, f"{video_id}_{tag}_{stamp}")
    try:
        page.screenshot(path=f"{base}.png", full_page=True)
        with open(f"{base}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        logger.warning("Wrote debug evidence to %s.png / %s.html", base, base)
    except Exception:
        logger.exception("Failed to write debug evidence for %s (%s)", video_id, tag)


def _click_first_visible(page, selectors: list, timeout_ms: int) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.click()
            return True
        except Exception:
            continue
    return False


def _extract_transcript_text(page, timeout_ms: int) -> str:
    """CONFIRMED against real markup via a live test's debug HTML dump
    (DcbP2eltVsE, see module docstring): each caption line is a
    <transcript-segment-view-model class="ytwTranscriptSegmentViewModelHost">
    element containing a timestamp div (class contains "...Timestamp") and
    the actual caption text in a separate
    <span class="ytAttributedStringHost ..."> -- e.g.:

        <transcript-segment-view-model class="ytwTranscriptSegmentViewModelHost" ...>
          <div aria-hidden="true" class="ytwTranscriptSegmentViewModelTimestamp">0:00</div>
          <div class="ytwTranscriptSegmentViewModelTimestampA11yLabel">0 seconds</div>
          <span class="ytAttributedStringHost ytAttributedStringLinkInheritColor" role="text">...actual text...</span>
        </transcript-segment-view-model>

    Targeting that span directly, scoped within each segment element, is
    more robust than grabbing the whole segment's innerText and trying to
    strip the timestamp back out -- ytAttributedStringHost is YouTube's
    generic text-rendering class used all over the site, so it's only safe
    to select scoped inside a transcript-segment-view-model, not page-wide.
    Falls back to the segment's own innerText if that span isn't found for
    some reason, rather than silently dropping the line."""
    segments = page.locator("transcript-segment-view-model")
    segments.first.wait_for(state="visible", timeout=timeout_ms)

    count = segments.count()
    texts = []
    for i in range(count):
        segment = segments.nth(i)
        text_span = segment.locator(".ytAttributedStringHost").first
        try:
            texts.append(text_span.inner_text())
        except Exception:
            texts.append(segment.inner_text())

    return " ".join(t.strip() for t in texts if t and t.strip())


def get_transcript_via_browser(video_id: str, browser) -> tuple:
    """Returns (transcript_text, note), same contract as
    home_transcript_fetcher.get_transcript. `browser` is a Playwright
    Browser instance, reused across videos in a run (launching a fresh
    browser per video would be wasteful -- one browser, one new page/tab
    per video, closed after)."""
    page = browser.new_page()
    try:
        page.goto(f"https://www.youtube.com/watch?v={video_id}", timeout=DEFAULT_PAGE_LOAD_TIMEOUT_MS)

        clicked = _click_first_visible(page, _SHOW_TRANSCRIPT_SELECTORS, DEFAULT_TRANSCRIPT_BUTTON_TIMEOUT_MS)
        if not clicked:
            # Maybe it's hidden behind an "...more" expand -- try that,
            # then retry the transcript button once.
            if _click_first_visible(page, _EXPAND_DESCRIPTION_SELECTORS, 3_000):
                clicked = _click_first_visible(page, _SHOW_TRANSCRIPT_SELECTORS, DEFAULT_TRANSCRIPT_BUTTON_TIMEOUT_MS)

        if not clicked:
            logger.info("video_id=%s: no 'Show transcript' button found -- treating as no captions", video_id)
            _dump_debug_evidence(page, video_id, "no_button")
            return "", _NOTE_NO_TRANSCRIPT_BUTTON

        try:
            transcript = _extract_transcript_text(page, DEFAULT_TRANSCRIPT_PANEL_TIMEOUT_MS)
        except Exception:
            logger.exception("video_id=%s: transcript button clicked but panel/text extraction failed", video_id)
            _dump_debug_evidence(page, video_id, "panel_extraction_failed")
            return "", _NOTE_PANEL_FOUND_BUT_EMPTY

        if not transcript:
            logger.info("video_id=%s: transcript panel opened but no text extracted", video_id)
            _dump_debug_evidence(page, video_id, "empty_after_extraction")
            return "", _NOTE_PANEL_FOUND_BUT_EMPTY

        logger.info("video_id=%s: extracted %d chars from transcript panel", video_id, len(transcript))
        return transcript, None
    finally:
        page.close()


def main():
    admin_api_url = os.environ.get("ADMIN_API_URL")
    token = os.environ.get("ADMIN_API_TOKEN")
    if not admin_api_url or not token:
        logger.error("ADMIN_API_URL and ADMIN_API_TOKEN must both be set -- see this script's module docstring for setup.")
        sys.exit(1)

    headless = os.environ.get("TRANSCRIPT_FETCHER_HEADLESS", "true").strip().lower() not in ("false", "0", "no")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("playwright isn't installed -- run: pip install -r scripts/requirements-browser.txt && playwright install chromium")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            summary = _run_with_admin_api(
                admin_api_url, token,
                get_transcript_fn=lambda video_id: get_transcript_via_browser(video_id, browser),
            )
        finally:
            browser.close()

    if summary["total"] > 0 and summary["errors"] == summary["total"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
