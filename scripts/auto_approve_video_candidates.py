#!/usr/bin/env python3
"""
Batch-approval helper for product_videos candidates discovered by
src/video_discovery/app.py.

WHY THIS EXISTS: video_discovery tags every candidate with match_confidence
('high' if the video title contains both the brand name and a significant
product-name token, 'low' otherwise -- see that function's score_match) but
never auto-approves anything itself, by design (see its module docstring:
"every candidate is still stored... this is exactly what the admin approval
step is for"). Approving one at a time via curl (DEPLOY_RUNBOOK.md 6i,
step 4) is fine for a handful of test videos but doesn't scale once
discovery is run across a whole catalog. This script automates the
low-risk half of that manual step (approving 'high' confidence matches)
and leaves the genuinely judgment-requiring half (the 'low' confidence
ones) for a human -- it never approves or rejects a 'low' match itself,
only prints them so you can decide with curl (or a future admin UI).

'high' confidence is still a simple heuristic, not a guarantee -- a title
like "Storm Absolute Power Review" would score 'high' for the "Storm
Absolute" product too (brand token "storm" + product token "absolute" both
present), even though the video is actually about a different ball. This
script doesn't attempt to fix that (a real fix belongs in score_match's
matching logic, not here) -- auto-approving 'high' trades a small,
accepted false-positive rate for not hand-reviewing every single obvious
match. If a wrong video slips through, reject it after the fact via
DEPLOY_RUNBOOK.md 6i's reject endpoint; nothing here is irreversible.

Talks to the same admin API as scripts/home_transcript_fetcher.py, same
env vars, same "import requests inside functions" convention so this
module can be imported for tests without requests installed.

Usage:
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
    python3 scripts/auto_approve_video_candidates.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("auto_approve_video_candidates")

DEFAULT_PAGE_LIMIT = 200
# Distinct from a real person's email on purpose -- resolved_by is this
# project's audit trail for "who decided this" (see every other approve/
# reject call in DEPLOY_RUNBOOK.md using an actual email), and an
# auto-approval isn't a human decision. Override via env var if you want
# something else in that column.
DEFAULT_RESOLVED_BY = "auto-approve-script (match_confidence=high)"


def list_pending_candidates(admin_api_url: str, token: str, page_limit: int = DEFAULT_PAGE_LIMIT) -> list:
    """Paginates GET /video-candidates?status=pending until a short page
    signals the end -- same pagination shape as
    home_transcript_fetcher.list_candidates_needing_transcripts."""
    import requests

    items = []
    offset = 0
    while True:
        resp = requests.get(
            f"{admin_api_url}/video-candidates",
            params={"status": "pending", "limit": page_limit, "offset": offset},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        resp.raise_for_status()
        page = resp.json().get("items", [])
        items.extend(page)
        if len(page) < page_limit:
            break
        offset += page_limit
    return items


def split_by_confidence(candidates: list) -> tuple:
    """Returns (high, low) -- everything else (an unexpected/missing
    match_confidence value) is treated as low rather than silently
    auto-approved, since 'not obviously high' should default to the
    human-review path, not the other way around."""
    high = [c for c in candidates if c.get("match_confidence") == "high"]
    low = [c for c in candidates if c.get("match_confidence") != "high"]
    return high, low


def approve_candidate(admin_api_url: str, token: str, video_id: str, resolved_by: str) -> None:
    import requests

    resp = requests.post(
        f"{admin_api_url}/video-candidates/{video_id}/approve",
        json={"resolved_by": resolved_by},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()


def _format_low_confidence_line(candidate: dict) -> str:
    return (
        f"  id={candidate.get('id')} product={candidate.get('brand_name')} {candidate.get('product_name')!r} "
        f"title={candidate.get('title')!r} channel={candidate.get('channel_title')!r} "
        f"query={candidate.get('match_query')!r}"
    )


def run(admin_api_url: str, token: str, resolved_by: str = DEFAULT_RESOLVED_BY,
        list_fn=None, approve_fn=None) -> dict:
    """Tolerates per-candidate approval errors the same way
    home_transcript_fetcher.run() tolerates per-video fetch errors -- one
    bad row (e.g. approved by someone else a second ago, a genuine race)
    shouldn't stop the rest of the batch."""
    list_candidates = list_fn if list_fn is not None else list_pending_candidates
    approve = approve_fn if approve_fn is not None else approve_candidate

    candidates = list_candidates(admin_api_url, token)
    high, low = split_by_confidence(candidates)
    logger.info("Found %d pending candidate(s): %d high confidence, %d low confidence", len(candidates), len(high), len(low))

    approved = 0
    errors = 0
    for candidate in high:
        video_id = candidate["id"]
        try:
            approve(admin_api_url, token, video_id, resolved_by)
            approved += 1
            logger.info("Approved id=%s title=%r", video_id, candidate.get("title"))
        except Exception:
            errors += 1
            logger.exception("Failed to approve id=%s title=%r -- left pending, will retry next run", video_id, candidate.get("title"))

    if low:
        logger.info("%d low-confidence candidate(s) left pending for manual review:", len(low))
        for candidate in low:
            logger.info(_format_low_confidence_line(candidate))

    return {"total_pending": len(candidates), "approved": approved, "errors": errors, "left_for_review": len(low)}


def main():
    admin_api_url = os.environ.get("ADMIN_API_URL")
    token = os.environ.get("ADMIN_API_TOKEN")
    if not admin_api_url or not token:
        logger.error("ADMIN_API_URL and ADMIN_API_TOKEN must both be set -- see this script's module docstring for setup.")
        sys.exit(1)

    resolved_by = os.environ.get("AUTO_APPROVE_RESOLVED_BY", DEFAULT_RESOLVED_BY)

    summary = run(admin_api_url, token, resolved_by=resolved_by)
    logger.info("Done: %s", summary)

    if summary["approved"] == 0 and summary["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
