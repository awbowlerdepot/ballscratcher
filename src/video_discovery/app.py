"""
YouTube content-enrichment discovery: searches YouTube for candidate review/
reaction videos per product and stores them in product_videos as 'pending'
for an admin to approve or reject via the admin API (see src/admin_api/
service.py's list_video_candidates/approve_video_candidate/
reject_video_candidate, and 004_product_videos.sql's module comment for why
this is a dedicated table rather than reusing review_queue).

Manual/direct invoke only, same convention as this project's other discovery
functions being invoked by hand rather than on a schedule (see
DEPLOY_RUNBOOK.md) -- there's no SQS trigger here. The invoking event
decides scope:
    {"product_ids": ["<uuid>", ...]}   -- specific products, any status/published value
    {"brand_id": "<uuid>"}             -- all 'current' (non-retired) products for one brand
    {}                                  -- all 'current' (non-retired) products (capped, see below)
    {"refresh_stats": true, "limit": N} -- re-pull view/like/comment counts (etc.) for up to N
                                            EXISTING product_videos rows instead of searching for
                                            new ones (see refresh_video_stats)
This is deliberately NOT "the whole catalog, always" -- the user explicitly
chose a subset-first approach over an immediate full-catalog job, and a
per-invocation scope argument is how every other discovery function in this
project already supports "just run it on what I tell you to" (e.g.
netsuite_url_discovery's BRAND_ID env var, commercebuild's per-brand loop).

VIDEO STATS (view/like/comment counts, duration, description): search.list's
snippet part -- everything search_youtube ever returns -- has no engagement
data at all. Al: "for the videos can we get pull down more data points from
the videos, date it was added current view counts and any other data that
make sense." A separate videos.list call (fetch_video_statistics) is what
actually has view/like/comment counts plus contentDetails.duration and the
full description; the search flow now calls it right after every
search.list call to enrich brand-new candidates at discovery time, and the
{"refresh_stats": true} job shape re-pulls it for candidates that already
exist (view counts are a snapshot, not a fixed fact -- they go stale the
moment they're written, unlike title/channel/published_at). videos.list is
NOT the constrained resource search.list is (see HARD QUOTA CONSTRAINT
below) -- it costs a small, flat number of quota units per call regardless
of how many of the up to 50 ids are packed into it, nowhere near
search.list's 100 units/call -- so neither of these needed their own
circuit breaker or the same conservative per-invocation cap.

SHORTS FILTER: Al, real incident found in production -- "the intent of
the video ingestion is review content and i have seen short popping up
and skewing things and there is no audible content at all. maybe we put
a duration requirment on videos." A YouTube Short has no real review
content (often no audio track at all), but nothing about search.list or
the match-confidence heuristic can tell one apart from a real review --
duration is the only reliable signal, and duration only becomes known
via the same videos.list enrichment call VIDEO STATS above already
makes. MIN_VIDEO_DURATION_SECONDS (61 -- Al's own choice over YouTube's
newer 3-minute Shorts-eligibility window, for the lower false-positive
risk against real quick-take content) is enforced in two places:
`filter_out_shorts` drops a confirmed Short before it's ever inserted as
a new candidate, and `apply_video_stats` force-rejects one at refresh
time -- including an already-'approved' row, Al's explicit "auto-reject
those too" choice, since a Short could easily have been approved before
anyone noticed the format (search.list's own results never carried
duration). A video whose duration isn't known yet is never treated as a
Short by either check -- see `is_likely_short`'s own docstring.

PRE-RELEASE FILTER (2026-09-04, CORRECTED 2026-09-06): Al, real ask after
seeing enough false-positive candidates in review -- "use the youtube
video date and the release date to auto reject youtube videos for balls
where the video was released prior to the ball release. it is almost
always likely to not match and be a similar name or a sibling." Original
implementation compared straight against products.release_date -- WRONG,
per Al's own correction after seeing legitimate videos get swept up:
"when i said that videos that fall before a date should be rejected
automatically i was referring to the announcement date not the release
date... reject them if they are from before the ball was announced or 45
days before the ball was released" (announced_date unknown for a given
product, which is the common case -- see 003_date_tracking_and_bowwwl.
sql's own comment on why no scraper populates it -- Al is comfortable
approximating "announced" as 45 days ahead of release rather than
requiring the real announcement date). Manufacturers routinely publish
preview/announcement coverage well before a ball is actually available
for sale, so comparing straight against release_date was rejecting real,
legitimate early coverage as if it were a mismatched sibling/prior-
generation ball -- exactly the false positives Al reported seeing.

Cutoff is now: `announced_date` when a product actually has one, else
`release_date - PRE_ANNOUNCEMENT_BUFFER_DAYS` (45) as a stand-in for "some
point before release a manufacturer might plausibly have announced this
ball" -- see `compute_earliest_valid_video_date`'s own docstring. A
candidate whose YouTube publishedAt predates that cutoff is almost always
about a similar-named product, a prior-generation ball, or a sibling in
the same line that happened to score a search-relevance hit, exactly as
Al described -- just measured against the right date now. `is_before_
release` is the pure comparison (see its own docstring for why it treats
an unknown cutoff as "never reject" -- release_date is frequently null,
see 003_date_tracking_and_bowwwl.sql's own header comment on how sparse
it is, and announced_date is unpopulated for every product today),
enforced in the same two places as the SHORTS FILTER above, for the same
reason:
  1. Discovery time (`filter_out_pre_release_videos`, called from
     handler right after the Shorts filter, before insert_candidates) --
     a pre-release candidate never becomes a product_videos row.
  2. Stats-refresh time (`apply_video_stats`, via the release_date/
     announced_date/published_at now joined into select_video_ids_
     needing_stats_refresh) -- force-rejects a row regardless of current
     status, including 'approved', same as the Shorts enforcement. This
     is what sweeps EXISTING candidates (stored before this filter
     existed, wrongly rejected under the old release_date-only logic, or
     whose product's release_date/announced_date only got backfilled/
     corrected later) into compliance the next time their stats happen
     to refresh, via the exact same scheduled refresh-stats trigger (see
     task #238) that already does this for Shorts -- no separate one-off
     backfill/un-reject script needed. NOTE: this only re-evaluates rows
     currently being force-rejected going forward -- a row this bug
     already incorrectly rejected before this fix shipped stays
     'rejected' until an admin manually restores it (see restore_video_
     candidate) or it happens to cycle through a stats refresh, since
     apply_video_stats only ever transitions TO 'rejected', never away
     from it.
Unlike the Shorts check, this one needs no NEW data fetch at refresh
time -- published_at is already known from discovery, and release_date/
announced_date are already known on products -- so it runs
unconditionally in apply_video_stats, not gated behind `if stats:` the
way the Shorts check is.

The default/brand_id scopes deliberately do NOT require `published = true`
(they only used to, until a real catalog check found why that mattered:
142 'current' products, but only 1 with `published = true` -- almost the
entire catalog would never get discovered under that filter). Video
discovery is meant to run ahead of publishing, per an explicit product
decision: candidates should already be found (and ideally approved) by the
time a product actually goes live, not discovered from scratch afterward.
`status = 'current'` (excluding retired balls) is still applied -- that
part of the scoping was never in question, only `published`.

ROTATION (real bug found in production, fixed via 005_products_last_
video_discovery_at.sql): the default/brand_id scopes used to order by
`p.updated_at desc` -- but nothing in this pipeline ever touches that
column, so every {} invocation re-selected the exact same top-N products,
forever. The documented "run {} once a day to cover the whole catalog
under the daily quota" pattern (DEPLOY_RUNBOOK.md 6i) never actually
progressed past the first day's batch. Fixed by ordering on
`last_video_discovery_at asc nulls first` instead: never-searched products
always sort first, and mark_product_searched (called from handler, success
path only -- see its docstring for why errors don't count) records when a
product was actually covered. Once the whole catalog has been searched at
least once, this naturally cycles back to the least-recently-searched
products, which is also correct behavior long-term (new review videos get
posted well after a ball's release).

HARD QUOTA CONSTRAINT (real, CONFIRMED by the user directly in Google Cloud
console -- not a units-math estimate -- same discipline as this project's
other real, disclosed constraints like the AWS account's 10-execution
Lambda concurrency ceiling): this project's YouTube Data API v3 quota is
configured for exactly 100 search.list calls/day (the console shows this
as a per-day query cap, consistent with the default 10,000-unit budget at
100 units/call, but the 100/day figure itself is the confirmed number, not
a derived one). This function makes exactly one search call per product.
MAX_SEARCHES_PER_INVOCATION defaults to 70 (not 100, and no longer 90 --
see REAL INCIDENT #3 below) to leave real same-day headroom for a retry or
follow-up run without exhausting the daily quota outright. If this needs
to cover more than ~70 products/day, that requires either a quota increase
request in the Google Cloud console or spreading invocations across
multiple days -- not something fixable in this code.

Match confidence is a simple two-tier heuristic (see score_match), not a
real relevance score -- YouTube's search API already ranks by its own
relevance signal, but a video titled just "bowling tips" for a query like
"Storm Absolute bowling ball" would still come back as a top result
sometimes. Rather than guess at a numeric threshold, every candidate is
still stored (never silently dropped), but tagged 'low' confidence when the
brand+product name aren't both recognizable in the title -- this is exactly
what the admin approval step is for.

QUERY WORDING (real incident, 2026-08-15): build_search_query used to
append "review" too ("<brand> <product> bowling ball review"). Al found a
concrete case where that word actively hid the right result -- Storm
Equinox Hybrid's actual #1 organic YouTube hit for the plain ball name
never showed up as a discovered candidate, because search.list's own
relevance ranking favors literal query-term matches and most real review/
reaction titles don't happen to contain the word "review" itself. Dropped
from the query (see build_search_query); "bowling ball" stays, since it's
a generic disambiguator rather than a term real review titles would
plausibly omit.

REAL INCIDENT, first 90-product run against the whole never-searched
backlog (Track/Ebonite's brand-new catalogs plus everything else that had
never been discovered): 813 new candidates, but 38 of the 90 searches
failed. CloudWatch showed every failure was the SAME cause -- a 429 from
search.list with reason "rateLimitExceeded" against quota metric "Search
Queries per minute", NOT the daily 10,000-unit ceiling described above
(90 calls is only 9,000 units, comfortably under that). This is a
DIFFERENT, shorter-window limit: YouTube throttles how fast you can call
search.list, not just how many calls/day -- and search_youtube() was
firing all 90 requests back-to-back with no pacing or retry at all, so
once the per-minute cap was hit partway through, every remaining call in
that burst got rejected. Same failure shape as the Shopify 429s and the
Lambda-concurrency 503s found earlier in this project (an unthrottled
loop outrunning someone else's rate limit) -- fixed the same way, retry-
with-backoff via get_youtube_requests_session() below, reusing the exact
RETRY_TOTAL/RETRY_BACKOFF_FACTOR/RETRY_STATUS_FORCELIST naming convention
scripts/backfill_core_ids.py already established for this. No data was
actually lost from the incident itself -- mark_product_searched only
runs on the success path, so all 38 failed products stayed
last_video_discovery_at=NULL and were still eligible for the very next
{} invocation -- but without this fix, a re-run risks hitting the same
wall again partway through. VideoDiscoveryFunction's Timeout in
template.yaml was also raised (150s -> 280s) to give the retry backoff
room to actually wait out a per-minute window without the Lambda itself
timing out mid-recovery.

REAL INCIDENT #2, the very next invocation after the fix above shipped:
"Sandbox.Timedout ... Task timed out after 280.00 seconds" -- a hard
Lambda kill, not a clean JSON response, meaning zero visibility into how
far it got. Root cause: retry-with-backoff fixes the case where a FEW
requests in a batch get throttled, but doesn't scale when MOST/ALL of
them do -- worst case per throttled product is ~31s of backoff (5
retries, 1s/2s/4s/8s/16s) plus request time, and since the loop is
strictly sequential, that cost stacks per product rather than overlapping.
This particular run likely started already deep into a rate-limited
window (two overlapping invocations from the CLI's own client-side read
timeout retrying mid-flight the run before this one -- a separate, CLI-
side issue: `aws lambda invoke`'s default --cli-read-timeout is 60s,
well under this function's now-280s server-side Timeout, so the CLI can
give up and silently retry a fresh invocation while the first one is
still legitimately running server-side, doubling request volume against
the same per-minute quota). Whatever the exact trigger, a batch that's
sustained-throttled from the start can blow past any Lambda Timeout you
set, since backoff cost scales with the number of throttled products,
not a fixed amount. Fixed with a circuit breaker (see
DEFAULT_CIRCUIT_BREAKER_THRESHOLD/_is_rate_limit_error/handler below):
5 consecutive 429s (specifically 429s, not any 5 consecutive failures --
an unrelated bug shouldn't trip a rate-limit breaker) stops the loop
early with a clean, informative result
({"circuit_breaker_tripped": true, "products_skipped": N, ...}) instead
of grinding through the remaining batch paying full backoff on every one.
Skipped products are, same as failed ones, still last_video_discovery_at
=NULL and naturally picked up by the next {} invocation.

REAL INCIDENT #3, two consecutive invocations after the circuit breaker
above shipped: both runs returned the IDENTICAL result --
{"new_candidates": 0, "search_errors": 5, "circuit_breaker_tripped": true,
"products_skipped": 85} -- byte-for-byte the same, on two separate
attempts. A per-minute rate limit should show variance run to run (some
partial progress before tripping, different error counts as the window
shifts); getting the exact same numbers twice pointed at a limit that
resets on a much longer cycle than a minute. The math also fit: the very
first successful run in REAL INCIDENT #1 alone made 90 calls = 9,000 of
the (assumed at the time) 10,000-unit daily budget, and every run after
that was landing on an already-near-exhausted day. Rather than ship
another blind code change, asked the user to check the actual configured
daily limit in Google Cloud console directly -- confirmed at exactly 100
search.list queries/day (see HARD QUOTA CONSTRAINT above). This meant
MAX_SEARCHES_PER_INVOCATION=90 was consuming nearly the ENTIRE daily
budget in a single invocation, leaving next to no room for the retries
and follow-up runs this project had already been attempting same-day.
Fixed by lowering the default to 70, leaving 30 units/day of real
headroom -- not a code bug, a capacity-planning one, and the same
"request a quota increase or spread the work out" ceiling documented
above still applies if 70 products/day stops being enough.
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_MAX_RESULTS_PER_PRODUCT = 5
# 70, not the confirmed 100/day ceiling itself -- see REAL INCIDENT #3 in
# the module docstring. A single invocation must leave same-day headroom
# for a retry/follow-up run; 90 (the old default) left only 10 units of
# slack, which is exactly what caused two consecutive circuit-breaker-
# tripped runs once the daily quota was already spent.
DEFAULT_MAX_SEARCHES_PER_INVOCATION = 70
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
# YouTube's own hard cap on how many comma-separated ids one videos.list
# call can request -- not a self-imposed one, unlike MAX_SEARCHES_PER_
# INVOCATION above.
MAX_VIDEO_IDS_PER_CALL = 50
# Manual-invoke default for the {"refresh_stats": true} job shape (see
# refresh_video_stats/handler). Sized for "a reasonable chunk of the
# table per run", not a quota-survival number the way MAX_SEARCHES_PER_
# INVOCATION is -- videos.list costs a small, flat number of units per
# call regardless of batch size, nowhere near search.list's 100 units/
# call (see module docstring's VIDEO STATS section).
DEFAULT_REFRESH_STATS_LIMIT = 200

# SHORTS FILTER: Al's ask -- "the intent of the video ingestion is
# review content and i have seen short popping up and skewing things and
# there is no audible content at all. maybe we put a duration
# requirment on videos." 61, not YouTube's newer expanded 3-minute
# Shorts-eligibility window -- Al's own choice between the two, picked
# for the lower false-positive risk (almost nothing with real review
# commentary runs under a minute; a lot of legitimate quick-take/first-
# impression content lives in the 1-3 minute range that the 3-minute
# cutoff would also catch). Enforced in two places, not one:
#   1. Discovery time (filter_out_shorts, called from handler before
#      insert_candidates) -- a confirmed Short never becomes a
#      product_videos row in the first place.
#   2. Stats-refresh time (apply_video_stats) -- Al's explicit choice:
#      duration is a HARD requirement regardless of past approval, so an
#      already-'approved' Short (likely approved before anyone noticed
#      the format, since search.list's own results never included
#      duration -- see the VIDEO STATS section above) gets auto-rejected
#      the next time its stats refresh, same as a still-'pending' one.
# A video whose duration ISN'T known yet (enrichment failed, or hasn't
# run yet) is never treated as a Short by either enforcement point --
# see is_likely_short's own docstring.
MIN_VIDEO_DURATION_SECONDS = 61

# Distinct from a real person's email on purpose, same reasoning as
# scripts/auto_approve_video_candidates.py's DEFAULT_RESOLVED_BY --
# resolved_by is this project's audit trail for "who decided this," and
# this is a rule, not a human decision.
SHORT_REJECTED_BY = f"video_discovery (duration < {MIN_VIDEO_DURATION_SECONDS}s, likely a Short)"

# PRE-RELEASE FILTER (see module docstring): a video published before
# the product's earliest-valid-video cutoff (announced_date, or
# release_date minus this many days when announced_date is unknown) is
# almost certainly about a different (similar-named/sibling/prior-
# generation) product, not this one.
#
# 45 days is Al's own number, given directly in his correction of this
# feature's original release_date-only logic: "reject them if they are
# from before the ball was announced or 45 days before the ball was
# released" -- a stand-in for "how far ahead of release a manufacturer
# might plausibly announce/preview a ball" when the real announced_date
# isn't known (which is every product today -- see 003_date_tracking_
# and_bowwwl.sql's own comment on why no scraper populates it).
PRE_ANNOUNCEMENT_BUFFER_DAYS = 45

PRE_RELEASE_REJECTED_BY = "video_discovery (published before product's announcement/pre-release window)"

# Retried status codes: 429 is the real, confirmed cause here (YouTube's
# per-minute search.list rate limit -- see module docstring's incident
# writeup), 500/502/503/504 included as the same "probably transient"
# bucket every other retry-enabled request in this project uses.
# backoff_factor=1 with urllib3's default formula
# (backoff_factor * 2^(retry_number-1)) waits 1s/2s/4s/8s/16s between the
# 5 attempts -- and, importantly, urllib3's Retry honors a Retry-After
# response header when present (respect_retry_after_header defaults to
# True), so a request that DOES get a real reset hint from Google waits
# exactly that long instead of guessing.
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)
RETRY_TOTAL = 5
RETRY_BACKOFF_FACTOR = 1

# Circuit breaker: real, confirmed second incident (see module docstring)
# -- per-request retry-with-backoff alone doesn't scale when MOST/ALL of
# an invocation's batch is rate-limited at once, since the up-to-~31s
# worst-case backoff per product (see RETRY_BACKOFF_FACTOR's comment)
# stacks sequentially across the whole loop. A batch that's sustained-
# throttled from the start can blow past even a generous Lambda Timeout
# with zero visibility into what happened (a hard Sandbox.Timedout kill,
# not a clean JSON response). 5 consecutive 429s (not just any 5
# consecutive failures -- see _is_rate_limit_error) is treated as "the
# per-minute window is exhausted for this whole invocation, not just
# unlucky for one product" -- stopping there instead of continuing to pay
# full backoff on every remaining product.
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 5

# Generic words stripped when building the "significant tokens" set used by
# score_match -- every product name contains these, so matching on them
# would make almost any bowling video "high confidence".
_STOPWORDS = {"bowling", "ball", "the", "and", "a", "an", "of", "-", "/"}
_WORD_RE = re.compile(r"[a-z0-9]+")


def significant_tokens(name: str) -> set:
    """Lowercased alphanumeric tokens from a product/brand name, minus
    generic bowling-catalog stopwords. Used by score_match to decide if a
    video title plausibly refers to this specific product, not just to
    bowling balls in general."""
    if not name:
        return set()
    return {tok for tok in _WORD_RE.findall(name.lower()) if tok not in _STOPWORDS}


# Cover/finish edition words that commonly distinguish separate catalog
# products sharing the same base name (e.g. "Phaze II" vs "Phaze II
# Pearl", sold as two distinct products with materially different
# performance). Real, confirmed incident (Al, 2026-09-06): a video
# titled "... Phaze II Pearl Review" scored 'high' against the SOLID
# "Phaze II" product too, since score_match only required ONE shared
# significant token and "pearl" isn't a stopword -- it got approved onto
# the wrong product, and product_article_generator then faithfully
# (and wrongly) wrote up the Pearl's performance as if it were this
# ball's. auto_approve_video_candidates.py's own module docstring
# already flagged this exact collision shape ("Storm Absolute Power"
# vs "Storm Absolute") as a known gap belonging in score_match, not
# there -- this is that fix.
#
# Deliberately a SMALLER set than product_article_generator's own
# _LINE_QUALIFIER_WORDS (which also has "plus", "pro", "max",
# "reactive") -- those are too generic/ambiguous to safely treat as a
# hard edition signal here ("reactive" in particular is just generic
# coverstock-material vocabulary that shows up in almost any ball
# review title, pearl or not; treating it as a mismatch signal would
# throw out far more good matches than it catches). This list is
# intentionally limited to words that reliably denote a specific,
# separately-sold finish/edition in this industry.
_EDITION_QUALIFIER_WORDS = {"solid", "pearl", "hybrid", "particle"}


def _title_names_different_edition(title_lower: str, product_name: str) -> bool:
    """True if the video title names a specific cover/finish edition
    (see _EDITION_QUALIFIER_WORDS) that this product's OWN name does not
    carry -- the collision pattern described above. Word-boundary
    matched so "pearl" doesn't false-positive on some unrelated
    substring, and only checked against product_name (not brand_name),
    since brand names don't carry edition words."""
    product_name_lower = (product_name or "").lower()
    for word in _EDITION_QUALIFIER_WORDS:
        pattern = rf"\b{word}\b"
        if re.search(pattern, title_lower) and not re.search(pattern, product_name_lower):
            return True
    return False


def score_match(title: str, brand_name: str, product_name: str) -> str:
    """Returns 'high' if the video title contains the brand name AND at
    least one significant token from the product name (AND doesn't name
    a different cover/finish edition than this product's own name --
    see _title_names_different_edition); 'low' otherwise. Deliberately
    permissive on the base-name check (any one product-name token, not
    all of them) since colorway suffixes like "Emerald/Black Hybrid" are
    often dropped from review video titles even when the video is a
    clear match for the base ball name -- the edition check above is
    what keeps that permissiveness from misfiring across two sibling
    products that share a base name but differ by finish."""
    if not title:
        return "low"
    title_lower = title.lower()

    brand_tokens = significant_tokens(brand_name)
    brand_hit = bool(brand_tokens) and any(tok in title_lower for tok in brand_tokens)

    product_tokens = significant_tokens(product_name)
    product_hit = bool(product_tokens) and any(tok in title_lower for tok in product_tokens)

    if brand_hit and product_hit and _title_names_different_edition(title_lower, product_name):
        return "low"

    return "high" if (brand_hit and product_hit) else "low"


def build_search_query(brand_name: str, product_name: str) -> str:
    # "review" dropped from the query text, 2026-08-15 -- real incident, Al:
    # "it is the extra words you added. i don't think that is necessary.
    # review is the word that breaks it and is not very commonly used to
    # describe youtube videos for balls." Confirmed case: Storm Equinox
    # Hybrid's actual #1 organic YouTube result for the plain ball name
    # never came back from search.list once "review" was appended --
    # search.list's relevance ranking favors literal query-term matches,
    # and plenty of real review/reaction video titles never use the word
    # "review" itself. "bowling ball" is kept -- it's a generic
    # disambiguator (keeps a short/ambiguous product name like "Bionic"
    # from pulling in unrelated non-bowling content) rather than a term
    # that competing review titles would plausibly omit, so it doesn't
    # have the same failure mode "review" did.
    return f"{brand_name} {product_name} bowling ball"


def get_youtube_requests_session():
    """Builds a requests.Session with urllib3 Retry mounted on https --
    see RETRY_STATUS_FORCELIST/RETRY_TOTAL/RETRY_BACKOFF_FACTOR's comment
    and the module docstring's incident writeup for why this exists (a
    real, confirmed per-minute YouTube rate limit, not a guess). A fresh
    session per call rather than one module-level singleton keeps this
    easy to monkeypatch/replace in tests without cross-test state -- same
    reasoning as scripts/backfill_core_ids.py's identically-named
    function. handler() below builds ONE session and reuses it across
    every product in a given invocation (connection pooling), rather than
    one per search_youtube call, but a fresh default is still provided
    here for any direct/manual call that doesn't pass one."""
    import requests
    from urllib3.util.retry import Retry

    session = requests.Session()
    retry = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUS_FORCELIST,
        allowed_methods=("GET",),
        raise_on_status=False,  # let the resp.ok check below report the final failure, not urllib3's own exception shape
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


def search_youtube(api_key: str, query: str, max_results: int = DEFAULT_MAX_RESULTS_PER_PRODUCT,
                    session=None) -> list:
    """One search.list call (100 quota units -- see module docstring).
    Returns a list of {youtube_video_id, title, channel_title, published_at,
    thumbnail_url} dicts. Kept separate from the DB/looping logic so tests
    can feed a canned response shape without a network call or a real key.
    session defaults to a fresh retry-enabled one (see
    get_youtube_requests_session) but is overridable so tests can inject a
    fake transport instead of hitting the network, and so handler() can
    pass down one shared session for the whole invocation."""
    import requests

    session = session if session is not None else get_youtube_requests_session()

    resp = session.get(
        YOUTUBE_SEARCH_URL,
        params={
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": max_results,
            "key": api_key,
            "safeSearch": "none",
        },
        timeout=30,
    )
    if not resp.ok:
        # Real gap found via this deploy's first live smoke test: a bare
        # resp.raise_for_status() only surfaces "403 Forbidden" in
        # CloudWatch, not WHY -- Google's error body (error.errors[0].reason,
        # e.g. "accessNotConfigured"/"keyInvalid"/"quotaExceeded") is the
        # actually-actionable part and was getting swallowed. Truncated to
        # keep a pathological response from bloating the log line; the key
        # itself is a request param, not part of the response body, so it
        # doesn't get echoed back into this message.
        raise requests.exceptions.HTTPError(
            f"{resp.status_code} error from YouTube search.list: {resp.text[:500]}",
            response=resp,
        )
    return parse_search_response(resp.json())


def parse_search_response(data: dict) -> list:
    videos = []
    for item in data.get("items", []):
        video_id = item.get("id", {}).get("videoId")
        if not video_id:
            continue
        snippet = item.get("snippet", {})
        thumbnails = snippet.get("thumbnails", {})
        thumbnail_url = (thumbnails.get("high") or thumbnails.get("default") or {}).get("url")
        videos.append({
            "youtube_video_id": video_id,
            "title": snippet.get("title"),
            "channel_title": snippet.get("channelTitle"),
            "published_at": snippet.get("publishedAt"),
            "thumbnail_url": thumbnail_url,
        })
    return videos


_DURATION_RE = re.compile(r"^PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$")


def parse_iso8601_duration(duration) -> int:
    """Parses contentDetails.duration (e.g. "PT4M13S", "PT1H2M3S", "PT45S")
    into whole seconds. Returns None for anything that isn't a match
    rather than raising -- duration is enrichment, the same "secondary
    data must not break the primary write" stance transcript_note already
    takes elsewhere in this pipeline (see module docstring's VIDEO STATS
    section)."""
    if not duration:
        return None
    match = _DURATION_RE.match(duration)
    if not match:
        return None
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return hours * 3600 + minutes * 60 + seconds


def is_likely_short(duration_seconds) -> bool:
    """True only when duration is KNOWN and under MIN_VIDEO_DURATION_
    SECONDS -- see the module docstring's SHORTS FILTER section for the
    full reasoning (Al's ask, the 61s cutoff choice, and the two
    enforcement points). A video whose duration hasn't been fetched yet
    (None -- the enrichment call itself failed, or hasn't run) is never
    treated as a Short here, same "secondary data must not block/change
    the primary outcome" stance transcript_note/the enrichment try/
    except already take elsewhere in this pipeline -- it just gets
    caught later, once its duration IS known, by whichever enforcement
    point (filter_out_shorts at discovery time, apply_video_stats at
    refresh time) next has the chance to check it."""
    return duration_seconds is not None and duration_seconds < MIN_VIDEO_DURATION_SECONDS


def filter_out_shorts(videos: list) -> list:
    """Drops candidates confirmed to be Shorts before they're ever
    inserted -- Al: "the intent of the video ingestion is review content
    and i have seen short popping up and skewing things and there is no
    audible content at all." Called from handler, after stats enrichment
    (so duration_seconds is populated when available) and before
    insert_candidates. A candidate whose duration isn't known yet is
    kept, not dropped -- see is_likely_short's own docstring."""
    return [v for v in videos if not is_likely_short(v.get("duration_seconds"))]


def compute_earliest_valid_video_date(release_date, announced_date):
    """Returns the earliest calendar date a legitimate review/preview
    video for this product could plausibly have been published, or None
    when neither date is known (see is_before_release's own docstring
    for why an unknown cutoff must never reject).

    Prefers announced_date when the product actually has one (the real,
    manufacturer-published concept -- see 003_date_tracking_and_bowwwl.
    sql), falling back to `release_date - PRE_ANNOUNCEMENT_BUFFER_DAYS`
    otherwise. This is Al's own correction of this filter's original
    release_date-only logic (2026-09-06): "when i said that videos that
    fall before a date should be rejected automatically i was referring
    to the announcement date not the release date... reject them if they
    are from before the ball was announced or 45 days before the ball
    was released you don't know that [the real announced_date]" --
    manufacturers routinely publish preview/announcement coverage weeks
    before a ball is actually available for sale, so comparing straight
    against release_date was rejecting that real, legitimate early
    coverage as if it were a mismatched sibling/prior-generation ball.

    announced_date is unpopulated for every product today (no scraper
    exposes a distinct announcement date -- see 003's own comment), so
    in practice this almost always falls through to the release_date
    branch, but the announced_date branch is correct and ready for
    whenever a real source turns up, or Al backfills one by hand."""
    if announced_date:
        return announced_date
    if release_date:
        return release_date - timedelta(days=PRE_ANNOUNCEMENT_BUFFER_DAYS)
    return None


def is_before_release(published_at, release_date, announced_date=None) -> bool:
    """True only when a cutoff date is known (see compute_earliest_
    valid_video_date) and the video's publish date is strictly earlier
    than it -- see the module docstring's PRE-RELEASE FILTER section for
    the full reasoning (Al's ask, why a video predating this cutoff is
    almost always a wrong match). Conservative both ways, same "unknown
    is never treated as disqualifying" posture as is_likely_short: a
    video with no known publish date, or a product with no computable
    cutoff (both release_date and announced_date unknown -- release_date
    is frequently null, see 003_date_tracking_and_bowwwl.sql), is never
    flagged here.

    published_at accepts either shape this pipeline actually produces:
    the raw ISO8601 string YouTube's search.list/videos.list responses
    use (discovery time, before a candidate is ever inserted) or a
    native datetime already parsed by psycopg2 (refresh time, read back
    from product_videos.published_at). release_date/announced_date are
    always a plain date (their respective products column types) or
    None. Same-calendar-day as the cutoff is NOT rejected (only strictly
    BEFORE is) -- a same-day launch/announcement review is a legitimate,
    if fast, real review."""
    cutoff = compute_earliest_valid_video_date(release_date, announced_date)
    if not published_at or not cutoff:
        return False
    if isinstance(published_at, str):
        try:
            published_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    published_date = published_at.date() if hasattr(published_at, "date") else published_at
    return published_date < cutoff


def filter_out_pre_release_videos(videos: list, release_date, announced_date=None) -> list:
    """Drops candidates whose published_at predates the product's
    earliest-valid-video cutoff (see is_before_release/compute_earliest_
    valid_video_date) -- called from handler right after the Shorts
    filter, before insert_candidates. Both dates unknown (the common
    case for announced_date -- see 003_date_tracking_and_bowwwl.sql on
    how sparse it and release_date both are) means every candidate is
    kept, same as a video with no known publish date."""
    return [v for v in videos if not is_before_release(v.get("published_at"), release_date, announced_date)]


def parse_video_details_response(data: dict) -> dict:
    """Returns {youtube_video_id: {view_count, like_count, comment_count,
    duration_seconds, description}}. An id YouTube doesn't return an item
    for (deleted/private video) is simply absent from the result rather
    than erroring -- see fetch_video_statistics's own docstring.
    statistics.likeCount/commentCount can be legitimately ABSENT (not
    zero) when a channel owner has hidden that count, which is why every
    numeric field here can end up None rather than 0 -- 0 would falsely
    claim "confirmed zero" for a number YouTube never actually
    disclosed."""
    def _to_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    results = {}
    for item in data.get("items", []):
        video_id = item.get("id")
        if not video_id:
            continue
        stats = item.get("statistics", {})
        content_details = item.get("contentDetails", {})
        snippet = item.get("snippet", {})
        results[video_id] = {
            "view_count": _to_int(stats.get("viewCount")),
            "like_count": _to_int(stats.get("likeCount")),
            "comment_count": _to_int(stats.get("commentCount")),
            "duration_seconds": parse_iso8601_duration(content_details.get("duration")),
            "description": snippet.get("description"),
        }
    return results


def fetch_video_statistics(api_key, video_ids: list, session=None) -> dict:
    """One or more videos.list calls (part=snippet,statistics,
    contentDetails, up to MAX_VIDEO_IDS_PER_CALL ids per call -- YouTube's
    own limit, not a self-imposed one) enriching search.list's bare title/
    channel/published/thumbnail with view/like/comment counts, duration,
    and the full description (see module docstring's VIDEO STATS
    section). Deliberately routed through the same retry-enabled session
    as search_youtube (see get_youtube_requests_session) -- not confirmed
    to share search.list's per-minute rate limit (see module docstring's
    incident writeup), but there's no reason to assume it's exempt
    either, and reusing the session costs nothing. Returns parse_video_
    details_response's shape merged across every batch; a video_id with
    nothing in the result just means YouTube didn't return an item for it
    (deleted/private), not that this call itself failed."""
    import requests

    session = session if session is not None else get_youtube_requests_session()
    results = {}
    for i in range(0, len(video_ids), MAX_VIDEO_IDS_PER_CALL):
        batch = video_ids[i:i + MAX_VIDEO_IDS_PER_CALL]
        resp = session.get(
            YOUTUBE_VIDEOS_URL,
            params={
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(batch),
                "key": api_key,
            },
            timeout=30,
        )
        if not resp.ok:
            raise requests.exceptions.HTTPError(
                f"{resp.status_code} error from YouTube videos.list: {resp.text[:500]}",
                response=resp,
            )
        results.update(parse_video_details_response(resp.json()))
    return results


def fetch_products_to_search(conn, job: dict, max_products: int) -> list:
    """Resolves the job's scope (see module docstring) into a list of
    {id, name, brand_name, release_date, announced_date} dicts, capped at
    max_products. 'current' (non-retired) products only, by default --
    retired balls are lower priority for review-video enrichment and can
    be added to product_ids explicitly if ever wanted. Deliberately does
    NOT require published = true (see module docstring's real catalog
    numbers on why that was dropped) -- discovery is meant to run ahead
    of publishing, not after it.

    release_date/announced_date (see module docstring's PRE-RELEASE
    FILTER section) are both frequently None -- announced_date is
    unpopulated for every product today -- handler() passes both
    straight through to filter_out_pre_release_videos, which treats no
    computable cutoff as "never reject", same as every other caller of
    is_before_release/compute_earliest_valid_video_date.

    Default/brand_id scopes order by last_video_discovery_at asc nulls
    first (see module docstring's ROTATION section) -- this is what makes
    repeated {} invocations actually progress through the catalog instead
    of re-selecting the same top-N products every time. product_ids scope
    ignores that column entirely (an explicit list is already a deliberate
    choice, not something to rotate) but still orders by p.id for
    deterministic results when len(product_ids) > max_products."""
    query = """
        select p.id, p.name, b.name as brand_name, p.release_date, p.announced_date
        from products p
        join brands b on b.id = p.brand_id
    """
    params = []
    conditions = []

    product_ids = job.get("product_ids")
    brand_id = job.get("brand_id")

    if product_ids:
        # Real bug found via this deploy's first live smoke test:
        # psycopg2 adapts a plain Python list to an untyped Postgres array
        # literal, which Postgres infers as text[] -- products.id is uuid,
        # and Postgres won't implicitly cast text[] to uuid[] for `= any()`
        # ("operator does not exist: uuid = text"). The explicit ::uuid[]
        # cast on the parameter (not the column) fixes it.
        conditions.append("p.id = any(%s::uuid[])")
        params.append(list(product_ids))
    else:
        conditions.append("p.status = 'current'")
        if brand_id:
            conditions.append("p.brand_id = %s")
            params.append(brand_id)

    if conditions:
        query += " where " + " and ".join(conditions)

    if product_ids:
        query += " order by p.id asc limit %s"
    else:
        query += " order by p.last_video_discovery_at asc nulls first, p.id asc limit %s"
    params.append(max_products)

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def mark_product_searched(conn, product_id: str) -> None:
    """Records that video_discovery actually completed a search.list call
    for this product -- called from handler's success path only (after
    search_youtube returns without raising), never from the except branch.
    A search that errors (transient network issue, or real: quota
    exhaustion -- see the module docstring's HARD QUOTA CONSTRAINT section,
    something this project has genuinely hit) never really searched
    anything; crediting it as 'searched' would push that product to the
    back of the rotation queue past products that were never attempted at
    all, which is exactly backwards. See fetch_products_to_search's
    ordering (last_video_discovery_at asc nulls first) for what this
    column drives."""
    with conn.cursor() as cur:
        cur.execute(
            "update products set last_video_discovery_at = now() where id = %s",
            (product_id,),
        )
    conn.commit()


def insert_candidates(conn, product_id: str, query: str, videos: list) -> int:
    """Inserts one product_videos row per video, tagged 'pending'.
    ON CONFLICT DO NOTHING makes this idempotent against re-running the same
    product (unique(product_id, youtube_video_id) from 004_product_videos.sql)
    -- a video already stored (in any status) is left untouched rather than
    reset back to pending.

    view_count/like_count/comment_count/duration_seconds/description/
    stats_fetched_at (migration 013) are all read via .get(), not direct
    indexing -- handler() populates them from fetch_video_statistics before
    calling this, but that enrichment call is wrapped in its own try/except
    (see handler's comment) specifically so a candidate still gets saved
    even when it fails, just without stats. Any caller (tests included)
    that passes a bare video dict without these keys still works, storing
    NULLs for all of them."""
    inserted = 0
    with conn.cursor() as cur:
        for video in videos:
            confidence = video["match_confidence"]
            cur.execute(
                """
                insert into product_videos
                    (product_id, youtube_video_id, title, channel_title,
                     published_at, thumbnail_url, match_query, match_confidence,
                     view_count, like_count, comment_count, duration_seconds,
                     description, stats_fetched_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (product_id, youtube_video_id) do nothing
                """,
                (
                    product_id, video["youtube_video_id"], video["title"],
                    video["channel_title"], video["published_at"],
                    video["thumbnail_url"], query, confidence,
                    video.get("view_count"), video.get("like_count"),
                    video.get("comment_count"), video.get("duration_seconds"),
                    video.get("description"), video.get("stats_fetched_at"),
                ),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted


def select_video_ids_needing_stats_refresh(conn, limit: int) -> list:
    """Rows ordered pv.stats_fetched_at asc nulls first, pv.id asc as a
    final tiebreaker (same determinism reasoning as admin_api/service.py's
    list_video_candidates' own id tiebreaker) -- never-refreshed candidates
    sort first, then the longest-stale ones, so repeated {"refresh_stats":
    true} invocations naturally cycle through the whole table the same way
    fetch_products_to_search's last_video_discovery_at ordering cycles
    through products.

    Now joins products for release_date/announced_date, and also returns
    this row's own published_at, so apply_video_stats can enforce the
    PRE-RELEASE FILTER (see module docstring) at refresh time without a
    second query -- none of these values depend on anything freshly
    fetched from YouTube, all are already sitting in the DB by the time
    this runs."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select pv.id, pv.youtube_video_id, pv.published_at, p.release_date, p.announced_date
            from product_videos pv
            join products p on p.id = pv.product_id
            order by pv.stats_fetched_at asc nulls first, pv.id asc
            limit %s
            """,
            (limit,),
        )
        return [
            {
                "id": row[0], "youtube_video_id": row[1], "published_at": row[2],
                "release_date": row[3], "announced_date": row[4],
            }
            for row in cur.fetchall()
        ]


def apply_video_stats(conn, video_pk: str, stats: dict, fetched_at, published_at=None, release_date=None, announced_date=None) -> None:
    """Updates one product_videos row by its own primary key, not
    youtube_video_id -- the same YouTube video can legitimately appear
    under more than one product_videos row (e.g. after reassign_video_
    candidate's copy-not-move behavior in admin_api/service.py), and each
    row's stats should refresh independently.

    stats={} (YouTube returned nothing for this id -- deleted/private
    video) still updates stats_fetched_at, so this row stops sorting
    first in select_video_ids_needing_stats_refresh's ordering forever,
    but deliberately leaves any previously-known view/like/comment/
    duration/description values untouched rather than nulling them out --
    a video that existed once and got taken down shouldn't lose its
    last-known numbers.

    SHORTS FILTER, refresh-time enforcement (see module docstring): when
    the freshly-fetched duration_seconds comes back under MIN_VIDEO_
    DURATION_SECONDS, this row is force-transitioned to status=
    'rejected' in a second update, regardless of its CURRENT status --
    including 'approved'. Al's explicit choice: duration is a hard
    requirement, not just a filter on brand-new candidates, so an
    already-'approved' Short (likely approved before anyone noticed the
    format, since search.list's own results never included duration)
    gets cleaned up automatically the next time its stats refresh, same
    as a still-'pending' one. Guarded with `and status <> 'rejected'` --
    a row a human already rejected for a real reason keeps that
    resolved_at/resolved_by untouched rather than being silently
    overwritten with the automated one.

    PRE-RELEASE FILTER, refresh-time enforcement (see module docstring):
    when published_at/release_date/announced_date (passed through from
    select_video_ids_needing_stats_refresh -- all already-known facts,
    nothing freshly fetched) show this row predates the product's
    earliest-valid-video cutoff (see compute_earliest_valid_video_date),
    it's force-transitioned to status='rejected' the same way and with
    the same guard, regardless of whether stats={} this refresh -- this
    check doesn't depend on the freshly-fetched stats at all, so it runs
    outside the `if stats:` branch, unlike the Shorts check above."""
    with conn.cursor() as cur:
        if stats:
            cur.execute(
                """
                update product_videos
                set view_count = %s, like_count = %s, comment_count = %s,
                    duration_seconds = %s, description = %s, stats_fetched_at = %s
                where id = %s
                """,
                (
                    stats.get("view_count"), stats.get("like_count"),
                    stats.get("comment_count"), stats.get("duration_seconds"),
                    stats.get("description"), fetched_at, video_pk,
                ),
            )
            if is_likely_short(stats.get("duration_seconds")):
                cur.execute(
                    """
                    update product_videos
                    set status = 'rejected', resolved_at = %s, resolved_by = %s
                    where id = %s and status <> 'rejected'
                    """,
                    (fetched_at, SHORT_REJECTED_BY, video_pk),
                )
        else:
            cur.execute(
                "update product_videos set stats_fetched_at = %s where id = %s",
                (fetched_at, video_pk),
            )
        if is_before_release(published_at, release_date, announced_date):
            cur.execute(
                """
                update product_videos
                set status = 'rejected', resolved_at = %s, resolved_by = %s
                where id = %s and status <> 'rejected'
                """,
                (fetched_at, PRE_RELEASE_REJECTED_BY, video_pk),
            )
    conn.commit()


def refresh_video_stats(conn, api_key, limit: int = DEFAULT_REFRESH_STATS_LIMIT, session=None) -> dict:
    """{"refresh_stats": true} job entry point (see handler) -- re-pulls
    current view/like/comment counts (and duration/description, which
    don't change but cost nothing extra to re-fetch in the same call) for
    up to `limit` existing product_videos rows, prioritizing rows that
    have never been fetched at all, then the longest-stale ones (see
    select_video_ids_needing_stats_refresh). Unlike the search flow, this
    has no per-request circuit breaker -- videos.list isn't confirmed to
    share search.list's per-minute rate limit (see fetch_video_
    statistics's docstring), and a single call already covers up to 50
    rows, so a `limit` of a few hundred is only a handful of HTTP calls
    total, not hundreds like the search flow's one-call-per-product
    shape. `candidates_rejected_as_shorts` in the return value (see
    apply_video_stats' own SHORTS FILTER section) is a running count of
    rows this call force-transitioned to status='rejected' -- includes
    ones that were 'approved' before this refresh, per Al's explicit
    "auto-reject those too" choice. `candidates_rejected_as_pre_release`
    is the same idea for the PRE-RELEASE FILTER (see module docstring)
    -- counted independently of whether stats_by_id had anything for
    that row this run, since published_at/release_date/announced_date
    never depend on the freshly-fetched stats."""
    session = session if session is not None else get_youtube_requests_session()
    rows = select_video_ids_needing_stats_refresh(conn, limit)
    if not rows:
        return {
            "candidates_checked": 0, "candidates_updated": 0,
            "candidates_rejected_as_shorts": 0, "candidates_rejected_as_pre_release": 0,
        }

    video_ids = [row["youtube_video_id"] for row in rows]
    stats_by_id = fetch_video_statistics(api_key, video_ids, session=session)
    fetched_at = datetime.now(timezone.utc)

    updated = 0
    rejected_as_shorts = 0
    rejected_as_pre_release = 0
    for row in rows:
        stats = stats_by_id.get(row["youtube_video_id"], {})
        apply_video_stats(
            conn, row["id"], stats, fetched_at,
            published_at=row.get("published_at"), release_date=row.get("release_date"),
            announced_date=row.get("announced_date"),
        )
        if stats:
            updated += 1
            if is_likely_short(stats.get("duration_seconds")):
                rejected_as_shorts += 1
        if is_before_release(row.get("published_at"), row.get("release_date"), row.get("announced_date")):
            rejected_as_pre_release += 1

    return {
        "candidates_checked": len(rows),
        "candidates_updated": updated,
        "candidates_rejected_as_pre_release": rejected_as_pre_release,
        "candidates_rejected_as_shorts": rejected_as_shorts,
    }


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


def get_youtube_api_key() -> str:
    """Reads the YouTube Data API v3 key from Secrets Manager
    (YOUTUBE_API_KEY_SECRET_ARN), mirroring how the DB credentials and the
    BigCommerce/admin-token secrets are fetched elsewhere in this project.
    The key itself has to come from the user (a Google Cloud console API
    key), same "credential this session can't fulfill itself" caveat as
    every other third-party secret in this project."""
    import boto3

    secret_arn = os.environ["YOUTUBE_API_KEY_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    secret_value = client.get_secret_value(SecretId=secret_arn)["SecretString"]
    try:
        return json.loads(secret_value)["api_key"]
    except (ValueError, KeyError):
        # Allow a plain-string secret too, not just a {"api_key": "..."} JSON blob.
        return secret_value


def _is_rate_limit_error(exc: Exception) -> bool:
    """True if exc is (ultimately) a 429 from search_youtube -- i.e. every
    retry in get_youtube_requests_session's Retry budget was already
    exhausted and the request STILL came back rate-limited, not just any
    exception. Duck-typed against exc.response.status_code rather than
    isinstance-checking requests.exceptions.HTTPError so this function
    doesn't need its own `import requests` -- every exception this
    project's request-based code raises on a bad HTTP response already
    carries a `.response` with a real status_code (see search_youtube's
    own raise), and anything else (a DB error, a bug, ...) just won't
    have one, which is exactly the "don't count this toward the breaker"
    case this function exists to distinguish."""
    return getattr(getattr(exc, "response", None), "status_code", None) == 429


def handler(event, context):
    job = event or {}

    api_key = get_youtube_api_key()
    # One session, reused across every product/call this invocation -- see
    # get_youtube_requests_session's docstring (connection pooling) and
    # the module docstring's incident writeup (this is also where the
    # actual retry-with-backoff protection against YouTube's per-minute
    # rate limit comes from; search_youtube's own default fallback exists
    # for direct/manual calls that skip handler entirely).
    session = get_youtube_requests_session()

    conn = get_db_connection()
    try:
        # {"refresh_stats": true} is a completely different job shape from
        # the search flow below -- no products to pick, no search.list
        # calls, no circuit breaker (see refresh_video_stats' own
        # docstring for why it doesn't need one) -- so it's handled as an
        # early return rather than threading a branch through the whole
        # search loop.
        if job.get("refresh_stats"):
            limit = int(job.get("limit") or DEFAULT_REFRESH_STATS_LIMIT)
            result = refresh_video_stats(conn, api_key, limit=limit, session=session)
            logger.info("Refreshed video stats: %s", result)
            return {"statusCode": 200, "body": json.dumps(result)}

        max_products = int(os.environ.get("MAX_SEARCHES_PER_INVOCATION", DEFAULT_MAX_SEARCHES_PER_INVOCATION))
        max_results = int(os.environ.get("MAX_RESULTS_PER_PRODUCT", DEFAULT_MAX_RESULTS_PER_PRODUCT))
        circuit_breaker_threshold = int(os.environ.get("CIRCUIT_BREAKER_THRESHOLD", DEFAULT_CIRCUIT_BREAKER_THRESHOLD))

        products = fetch_products_to_search(conn, job, max_products)
        logger.info("Searching YouTube for %d product(s) (cap=%d)", len(products), max_products)

        total_candidates = 0
        errors = []
        consecutive_rate_limit_failures = 0
        circuit_breaker_tripped = False
        products_attempted = 0

        for product in products:
            products_attempted += 1
            query = build_search_query(product["brand_name"], product["name"])
            try:
                videos = search_youtube(api_key, query, max_results, session=session)
            except Exception as exc:
                logger.exception("YouTube search failed for product_id=%s query=%r", product["id"], query)
                errors.append(product["id"])
                if _is_rate_limit_error(exc):
                    consecutive_rate_limit_failures += 1
                else:
                    # A non-rate-limit failure (bad query, transient DB
                    # hiccup, etc.) doesn't indicate a sustained quota
                    # wall the way repeated 429s do -- doesn't reset
                    # progress toward tripping, but doesn't count toward
                    # it either.
                    pass
                if consecutive_rate_limit_failures >= circuit_breaker_threshold:
                    circuit_breaker_tripped = True
                    logger.error(
                        "Circuit breaker tripped: %d consecutive rate-limit failures -- "
                        "stopping early with %d/%d product(s) attempted this invocation. "
                        "The remaining products were never touched (no last_video_discovery_at "
                        "update), so they'll be picked up again by the next {} invocation once "
                        "YouTube's per-minute quota window has actually reset.",
                        consecutive_rate_limit_failures, products_attempted, len(products),
                    )
                    break
                continue

            consecutive_rate_limit_failures = 0
            for video in videos:
                video["match_confidence"] = score_match(video["title"], product["brand_name"], product["name"])

            # Enrich with view/like/comment counts, duration, and
            # description -- search.list's snippet part never includes
            # these (see module docstring's VIDEO STATS section), a
            # separate videos.list call is the only way to get them.
            # Wrapped in its own try/except: a candidate is still worth
            # saving even if this enrichment call fails (rate limit,
            # transient network), same "secondary data must not block the
            # primary write" stance transcript_note already takes
            # elsewhere in this pipeline. Al: "pull down more data points
            # from the videos, date it was added current view counts and
            # any other data that make sense."
            video_ids = [v["youtube_video_id"] for v in videos]
            if video_ids:
                try:
                    stats_by_id = fetch_video_statistics(api_key, video_ids, session=session)
                    fetched_at = datetime.now(timezone.utc)
                    for video in videos:
                        video.update(stats_by_id.get(video["youtube_video_id"], {}))
                        video["stats_fetched_at"] = fetched_at
                except Exception:
                    logger.exception(
                        "Failed to fetch video statistics for product_id=%s (candidates will "
                        "still be saved without view/like/comment/duration data)",
                        product["id"],
                    )

            # SHORTS FILTER (see module docstring) -- drop confirmed
            # Shorts before they ever become a product_videos row. Runs
            # after enrichment, above, so duration_seconds is populated
            # whenever the videos.list call succeeded; a candidate whose
            # duration is still unknown (enrichment failed) passes
            # through untouched (see filter_out_shorts/is_likely_short).
            videos_before_shorts_filter = len(videos)
            videos = filter_out_shorts(videos)
            if len(videos) < videos_before_shorts_filter:
                logger.info(
                    "Dropped %d likely-Short candidate(s) for product_id=%s (duration < %ds)",
                    videos_before_shorts_filter - len(videos), product["id"], MIN_VIDEO_DURATION_SECONDS,
                )

            # PRE-RELEASE FILTER (see module docstring) -- drop candidates
            # published before this product's earliest-valid-video cutoff
            # (announced_date, or release_date minus PRE_ANNOUNCEMENT_
            # BUFFER_DAYS when announced_date is unknown, which is every
            # product today -- see fetch_products_to_search's own
            # docstring). No computable cutoff means filter_out_pre_
            # release_videos is a no-op, same as when a candidate's own
            # published_at is unknown.
            videos_before_pre_release_filter = len(videos)
            videos = filter_out_pre_release_videos(videos, product.get("release_date"), product.get("announced_date"))
            if len(videos) < videos_before_pre_release_filter:
                cutoff = compute_earliest_valid_video_date(product.get("release_date"), product.get("announced_date"))
                logger.info(
                    "Dropped %d pre-release candidate(s) for product_id=%s (published before cutoff=%s)",
                    videos_before_pre_release_filter - len(videos), product["id"], cutoff,
                )

            inserted = insert_candidates(conn, product["id"], query, videos)
            mark_product_searched(conn, product["id"])
            total_candidates += inserted
            logger.info(
                "product_id=%s query=%r -> %d results, %d new candidates",
                product["id"], query, len(videos), inserted,
            )
    finally:
        conn.close()

    return {
        "statusCode": 200,
        "body": json.dumps({
            "products_searched": len(products),
            "new_candidates": total_candidates,
            "search_errors": len(errors),
            "circuit_breaker_tripped": circuit_breaker_tripped,
            "products_skipped": len(products) - products_attempted,
        }),
    }
