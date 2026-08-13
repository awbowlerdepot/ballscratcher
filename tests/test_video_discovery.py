"""
Tests for src/video_discovery/app.py.

Manual-runner pattern, run standalone via
`python3 tests/test_video_discovery.py`.

Honesty note (same as test_admin_api_service.py): no real YouTube Data API
v3 response was captured this session (this sandbox has no outbound
network access, and a real API key wasn't available either) -- the sample
response shape in SAMPLE_SEARCH_RESPONSE below is built from Google's own
published search.list response documentation, not a live capture, so
parse_search_response's exact field paths should be double-checked against
a real response the first time this actually runs. Everything DB-facing
(fetch_products_to_search, insert_candidates) is exercised against a fake
psycopg2-shaped cursor/connection, same reasoning and same limitation as
every other *_url_discovery test file in this project: no Postgres
instance available in this sandbox.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "video_discovery"))

import app  # noqa: E402
import requests  # noqa: E402 -- real module; only used for the HTTPError exception type below


# --- significant_tokens / score_match / build_search_query: pure, no DB ---

def test_significant_tokens_strips_stopwords():
    assert app.significant_tokens("Storm Absolute Bowling Ball") == {"storm", "absolute"}


def test_significant_tokens_empty_for_blank_name():
    assert app.significant_tokens("") == set()
    assert app.significant_tokens(None) == set()


def test_score_match_high_when_brand_and_product_both_in_title():
    assert app.score_match("Storm Absolute Bowling Ball Review!", "Storm", "Absolute") == "high"


def test_score_match_low_when_title_only_generic():
    assert app.score_match("Top 10 bowling balls for hook", "Storm", "Absolute") == "low"


def test_score_match_low_when_only_brand_present():
    assert app.score_match("Storm bowling ball roundup", "Storm", "Absolute") == "low"


def test_score_match_permissive_on_colorway_suffix():
    """A review titled without the colorway suffix should still score
    'high' -- see score_match's docstring for why this is deliberately
    permissive (any one product-name token, not all of them)."""
    assert app.score_match("Storm Fury Review", "Storm", "Fury Emerald/Black Hybrid") == "high"


def test_score_match_low_for_blank_title():
    assert app.score_match("", "Storm", "Absolute") == "low"
    assert app.score_match(None, "Storm", "Absolute") == "low"


def test_build_search_query():
    assert app.build_search_query("Storm", "Absolute") == "Storm Absolute bowling ball review"


# --- parse_search_response: shape documented by Google, not live-captured (see module docstring) ---

SAMPLE_SEARCH_RESPONSE = {
    "items": [
        {
            "id": {"kind": "youtube#video", "videoId": "abc123XYZ"},
            "snippet": {
                "title": "Storm Absolute Bowling Ball Review",
                "channelTitle": "Bowling Review Channel",
                "publishedAt": "2026-01-15T12:00:00Z",
                "thumbnails": {
                    "default": {"url": "https://i.ytimg.com/vi/abc123XYZ/default.jpg"},
                    "high": {"url": "https://i.ytimg.com/vi/abc123XYZ/hqdefault.jpg"},
                },
            },
        },
        {
            # No videoId -- e.g. a channel or playlist result slipping through
            # despite type=video -- must be skipped, not crash.
            "id": {"kind": "youtube#channel", "channelId": "someChannel"},
            "snippet": {"title": "Some Channel"},
        },
    ],
}


def test_parse_search_response_extracts_real_shaped_fields():
    videos = app.parse_search_response(SAMPLE_SEARCH_RESPONSE)
    assert len(videos) == 1
    v = videos[0]
    assert v["youtube_video_id"] == "abc123XYZ"
    assert v["title"] == "Storm Absolute Bowling Ball Review"
    assert v["channel_title"] == "Bowling Review Channel"
    assert v["published_at"] == "2026-01-15T12:00:00Z"
    assert v["thumbnail_url"] == "https://i.ytimg.com/vi/abc123XYZ/hqdefault.jpg"


def test_parse_search_response_skips_non_video_items():
    videos = app.parse_search_response(SAMPLE_SEARCH_RESPONSE)
    assert all(v["youtube_video_id"] != None for v in videos)  # noqa: E711
    assert len(videos) == 1


def test_parse_search_response_empty_items():
    assert app.parse_search_response({"items": []}) == []
    assert app.parse_search_response({}) == []


# --- search_youtube error handling: real gap found via live smoke test ---
# (a bare resp.raise_for_status() only logged "403 Forbidden" in CloudWatch,
# not Google's actual error reason -- see app.py's fix comment).
#
# search_youtube now takes an injectable `session` param (see
# get_youtube_requests_session's docstring: a real, confirmed 429-storm
# from YouTube's per-minute rate limit during a live full-backlog run --
# see app.py's module docstring) rather than importing `requests` and
# calling requests.get directly, so these two tests inject a small fake
# session object with .get() instead of monkeypatching the module-level
# requests.get the way this file originally did -- get_youtube_requests_
# session() itself now touches requests.Session/requests.adapters.
# HTTPAdapter/urllib3.util.retry.Retry, none of which a bare
# requests.get swap would exercise, so injecting a fake session is both
# simpler and actually tests what search_youtube does with it. Same
# transformation scripts/backfill_core_ids.py's tests went through
# earlier for the identical reason.

class _FakeResponse:
    def __init__(self, status_code, text, ok):
        self.status_code = status_code
        self.text = text
        self.ok = ok

    def json(self):
        import json
        return json.loads(self.text)


class _FakeSession:
    def __init__(self, response):
        # response: either one response object (reused every call) or a
        # list of responses (consumed in order -- used by fetch_video_
        # statistics' batching tests, where MAX_VIDEO_IDS_PER_CALL forces
        # more than one videos.list call for a single invocation).
        if isinstance(response, list):
            self._responses = list(response)
            self._response = None
        else:
            self._responses = None
            self._response = response
        self.get_calls = []

    def get(self, url, params=None, timeout=None):
        self.get_calls.append({"url": url, "params": params})
        if self._responses is not None:
            return self._responses.pop(0)
        return self._response


def test_search_youtube_raises_with_response_body_on_error():
    fake = _FakeSession(_FakeResponse(
        403, '{"error": {"errors": [{"reason": "accessNotConfigured"}]}}', ok=False,
    ))
    try:
        app.search_youtube("fake-key", "some query", session=fake)
        assert False, "expected HTTPError"
    except requests.exceptions.HTTPError as e:
        assert "403" in str(e)
        assert "accessNotConfigured" in str(e)


def test_search_youtube_returns_videos_on_success():
    fake = _FakeSession(_FakeResponse(
        200,
        '{"items": [{"id": {"videoId": "abc"}, "snippet": {"title": "t", "channelTitle": "c", '
        '"publishedAt": "2026-01-01T00:00:00Z", "thumbnails": {"high": {"url": "https://x/y.jpg"}}}}]}',
        ok=True,
    ))
    videos = app.search_youtube("fake-key", "some query", session=fake)
    assert len(videos) == 1
    assert videos[0]["youtube_video_id"] == "abc"
    assert fake.get_calls[0]["params"]["q"] == "some query"


# --- get_youtube_requests_session: confirms the actual retry config,
# against the real requests/urllib3 (both available in this sandbox) --
# not just that the function runs, but that it retries on exactly the
# status this whole feature exists for (429, YouTube's per-minute rate
# limit -- see module docstring's incident writeup), plus the neighboring
# 5xx bucket, the expected retry count/backoff, and GET specifically
# (the only method this module ever calls).

def test_get_youtube_requests_session_retries_on_rate_limit_and_5xx():
    session = app.get_youtube_requests_session()
    adapter = session.get_adapter("https://www.googleapis.com/youtube/v3/search")
    retry = adapter.max_retries

    assert retry.total == app.RETRY_TOTAL
    assert set(retry.status_forcelist) == set(app.RETRY_STATUS_FORCELIST)
    assert 429 in retry.status_forcelist  # the specific status this feature exists for
    assert "GET" in retry.allowed_methods
    assert retry.backoff_factor == app.RETRY_BACKOFF_FACTOR


def test_get_youtube_requests_session_returns_a_fresh_session_each_call():
    assert app.get_youtube_requests_session() is not app.get_youtube_requests_session()


# --- parse_iso8601_duration: contentDetails.duration -> whole seconds ---

def test_parse_iso8601_duration_minutes_and_seconds():
    assert app.parse_iso8601_duration("PT4M13S") == 4 * 60 + 13


def test_parse_iso8601_duration_hours_minutes_seconds():
    assert app.parse_iso8601_duration("PT1H2M3S") == 3600 + 2 * 60 + 3


def test_parse_iso8601_duration_seconds_only():
    assert app.parse_iso8601_duration("PT45S") == 45


def test_parse_iso8601_duration_hours_only():
    assert app.parse_iso8601_duration("PT1H") == 3600


def test_parse_iso8601_duration_blank_or_unparseable_returns_none():
    assert app.parse_iso8601_duration(None) is None
    assert app.parse_iso8601_duration("") is None
    assert app.parse_iso8601_duration("not a duration") is None


# --- is_likely_short / filter_out_shorts: SHORTS FILTER (Al's ask -- "the
# intent of the video ingestion is review content and i have seen short
# popping up and skewing things and there is no audible content at all.
# maybe we put a duration requirment on videos"). 61s cutoff, Al's own
# choice over YouTube's newer 3-minute Shorts window (see module
# docstring's SHORTS FILTER section for the full reasoning). ---

def test_is_likely_short_true_under_the_cutoff():
    assert app.is_likely_short(60) is True
    assert app.is_likely_short(1) is True
    assert app.is_likely_short(0) is True


def test_is_likely_short_false_at_and_above_the_cutoff():
    assert app.is_likely_short(app.MIN_VIDEO_DURATION_SECONDS) is False  # boundary: 61 itself is NOT a Short
    assert app.is_likely_short(120) is False


def test_is_likely_short_false_when_duration_unknown():
    """Unknown duration (enrichment failed or hasn't run) is never
    treated as a Short -- same 'secondary data must not block/change the
    primary outcome' stance the rest of this pipeline already takes."""
    assert app.is_likely_short(None) is False


def test_filter_out_shorts_drops_only_confirmed_shorts():
    videos = [
        {"youtube_video_id": "v1", "duration_seconds": 30},   # Short -- dropped
        {"youtube_video_id": "v2", "duration_seconds": 120},  # real length -- kept
        {"youtube_video_id": "v3", "duration_seconds": None},  # unknown -- kept
        {"youtube_video_id": "v4"},                             # key absent entirely -- kept
    ]
    kept = app.filter_out_shorts(videos)
    assert [v["youtube_video_id"] for v in kept] == ["v2", "v3", "v4"]


def test_filter_out_shorts_empty_list():
    assert app.filter_out_shorts([]) == []


# --- parse_video_details_response / fetch_video_statistics: videos.list,
# the only call that ever returns view/like/comment counts (search.list's
# snippet part never does -- see module docstring's VIDEO STATS section).

SAMPLE_VIDEOS_LIST_RESPONSE = {
    "items": [
        {
            "id": "abc123XYZ",
            "snippet": {"description": "Full review of the Storm Absolute."},
            "statistics": {"viewCount": "12345", "likeCount": "678", "commentCount": "9"},
            "contentDetails": {"duration": "PT4M13S"},
        },
        {
            # likeCount/commentCount omitted -- real YouTube behavior when a
            # channel owner hides those counts (NOT the same as zero, see
            # parse_video_details_response's own comment).
            "id": "hiddenCounts1",
            "snippet": {"description": "A video with hidden engagement counts."},
            "statistics": {"viewCount": "500"},
            "contentDetails": {"duration": "PT2M"},
        },
    ],
}


def test_parse_video_details_response_extracts_stats_and_duration():
    results = app.parse_video_details_response(SAMPLE_VIDEOS_LIST_RESPONSE)
    assert results["abc123XYZ"] == {
        "view_count": 12345, "like_count": 678, "comment_count": 9,
        "duration_seconds": 253, "description": "Full review of the Storm Absolute.",
    }


def test_parse_video_details_response_hidden_counts_are_none_not_zero():
    results = app.parse_video_details_response(SAMPLE_VIDEOS_LIST_RESPONSE)
    hidden = results["hiddenCounts1"]
    assert hidden["view_count"] == 500
    assert hidden["like_count"] is None
    assert hidden["comment_count"] is None


def test_parse_video_details_response_empty_items():
    assert app.parse_video_details_response({"items": []}) == {}
    assert app.parse_video_details_response({}) == {}


def test_fetch_video_statistics_single_batch():
    fake = _FakeSession(_FakeResponse(200, json.dumps(SAMPLE_VIDEOS_LIST_RESPONSE), ok=True))
    results = app.fetch_video_statistics("fake-key", ["abc123XYZ", "hiddenCounts1"], session=fake)

    assert len(fake.get_calls) == 1
    assert fake.get_calls[0]["url"] == app.YOUTUBE_VIDEOS_URL
    assert fake.get_calls[0]["params"]["id"] == "abc123XYZ,hiddenCounts1"
    assert results["abc123XYZ"]["view_count"] == 12345


def test_fetch_video_statistics_batches_over_max_ids_per_call():
    """MAX_VIDEO_IDS_PER_CALL (50) is YouTube's own hard limit on ids per
    videos.list call, not a self-imposed one -- more than 50 ids must
    become more than one HTTP call."""
    video_ids = [f"v{i}" for i in range(75)]  # 75 -> 2 batches (50 + 25)
    responses = [
        _FakeResponse(200, json.dumps({"items": [{"id": "v0", "statistics": {}, "contentDetails": {}, "snippet": {}}]}), ok=True),
        _FakeResponse(200, json.dumps({"items": [{"id": "v50", "statistics": {}, "contentDetails": {}, "snippet": {}}]}), ok=True),
    ]
    fake = _FakeSession(responses)
    results = app.fetch_video_statistics("fake-key", video_ids, session=fake)

    assert len(fake.get_calls) == 2
    assert fake.get_calls[0]["params"]["id"] == ",".join(video_ids[:50])
    assert fake.get_calls[1]["params"]["id"] == ",".join(video_ids[50:])
    assert "v0" in results and "v50" in results


def test_fetch_video_statistics_raises_with_response_body_on_error():
    fake = _FakeSession(_FakeResponse(403, '{"error": {"errors": [{"reason": "accessNotConfigured"}]}}', ok=False))
    try:
        app.fetch_video_statistics("fake-key", ["abc"], session=fake)
        assert False, "expected HTTPError"
    except requests.exceptions.HTTPError as e:
        assert "403" in str(e)
        assert "accessNotConfigured" in str(e)


# --- Fake DB layer: fetch_products_to_search / insert_candidates ---

class _FakeCursor:
    """Mimics enough of psycopg2's cursor to exercise the DB-facing
    functions below: fetchall()+description for fetch_products_to_search's
    select and select_video_ids_needing_stats_refresh's select, a
    rowcount-based on-conflict-do-nothing simulation for insert_candidates,
    and recorded UPDATE params for mark_product_searched/apply_video_stats."""

    def __init__(self, products=None, known_pairs=None, refresh_rows=None):
        self.products = products or []
        self.known_pairs = known_pairs or set()
        self.refresh_rows = refresh_rows or []
        self.executed = []
        self.description = None
        self.rowcount = 0
        self._rows = []
        self.last_marked_searched = None
        self.stats_updates = []  # list of (video_pk, params) for the full-stats UPDATE branch
        self.stats_fetched_at_only_updates = []  # list of video_pk for the no-stats UPDATE branch
        self.short_reject_updates = []  # list of (fetched_at, resolved_by, video_pk) for the SHORTS FILTER reject branch

    def execute(self, query, params=None):
        params = params or []
        q = " ".join(query.split())
        self.executed.append((q, params))

        if q.startswith("select p.id, p.name, b.name as brand_name"):
            self.description = [("id",), ("name",), ("brand_name",)]
            self._rows = [(p["id"], p["name"], p["brand_name"]) for p in self.products]
        elif q.startswith("select id, youtube_video_id from product_videos"):
            self.description = [("id",), ("youtube_video_id",)]
            self._rows = [(r["id"], r["youtube_video_id"]) for r in self.refresh_rows]
        elif q.startswith("insert into product_videos"):
            product_id, youtube_video_id = params[0], params[1]
            key = (product_id, youtube_video_id)
            if key in self.known_pairs:
                self.rowcount = 0
            else:
                self.known_pairs.add(key)
                self.rowcount = 1
        elif q.startswith("update products set last_video_discovery_at"):
            self.last_marked_searched = params[0]
            self._last_result = None
        elif q.startswith("update product_videos set view_count"):
            self.stats_updates.append(params)
        elif q.startswith("update product_videos set status = 'rejected'"):
            self.short_reject_updates.append(params)
        elif q.startswith("update product_videos set stats_fetched_at"):
            self.stats_fetched_at_only_updates.append(params[-1])
        else:
            raise NotImplementedError(f"FakeCursor doesn't support: {q}")

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, products=None, known_pairs=None, refresh_rows=None):
        self._cursor = _FakeCursor(products, known_pairs, refresh_rows)
        self.committed = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def test_fetch_products_to_search_defaults_to_current_status_only():
    """Real catalog check found 142 'current' products but only 1 with
    published=true -- requiring published=true in this default scope would
    have meant video discovery basically never ran across the catalog, so
    that requirement was dropped (status='current' is still applied).
    See app.py's module docstring for the full reasoning."""
    conn = _FakeConn(products=[{"id": "p1", "name": "Absolute", "brand_name": "Storm"}])
    products = app.fetch_products_to_search(conn, {}, max_products=90)

    assert products == [{"id": "p1", "name": "Absolute", "brand_name": "Storm"}]
    query, params = conn.cursor().executed[0]
    assert "p.published = true" not in query
    assert "p.status = 'current'" in query
    assert params[-1] == 90  # the limit


def test_fetch_products_to_search_default_scope_rotates_never_searched_first():
    """Real production bug: the old `order by p.updated_at desc` never
    advanced (nothing touches updated_at), so {} kept re-selecting the same
    top-N products forever -- see mark_product_searched and
    005_products_last_video_discovery_at.sql. last_video_discovery_at asc
    nulls first is what actually rotates: never-searched products (null)
    always sort ahead of any previously-searched one, and p.id is the
    final deterministic tiebreaker (same discipline as the pv.id fix in
    list_video_candidates)."""
    conn = _FakeConn(products=[{"id": "p1", "name": "Absolute", "brand_name": "Storm"}])
    app.fetch_products_to_search(conn, {}, max_products=90)

    query, _ = conn.cursor().executed[0]
    assert "order by p.last_video_discovery_at asc nulls first, p.id asc limit %s" in query


def test_fetch_products_to_search_explicit_product_ids_skips_status_filter():
    conn = _FakeConn(products=[{"id": "p1", "name": "Absolute", "brand_name": "Storm"}])
    app.fetch_products_to_search(conn, {"product_ids": ["p1"]}, max_products=90)

    query, params = conn.cursor().executed[0]
    assert "p.id = any(" in query
    assert "p.published = true" not in query
    assert "p.status = 'current'" not in query


def test_fetch_products_to_search_product_ids_scope_orders_by_id_not_rotation():
    """product_ids is an explicit, deliberate list -- it shouldn't rotate
    (that column is about which products haven't been chosen yet, not
    relevant once someone's already named exactly which ones they want),
    but it still needs SOME deterministic order for when
    len(product_ids) > max_products."""
    conn = _FakeConn(products=[{"id": "p1", "name": "Absolute", "brand_name": "Storm"}])
    app.fetch_products_to_search(conn, {"product_ids": ["p1"]}, max_products=90)

    query, _ = conn.cursor().executed[0]
    assert "order by p.id asc limit %s" in query
    assert "last_video_discovery_at" not in query


def test_fetch_products_to_search_brand_id_scope_also_skips_published():
    conn = _FakeConn(products=[{"id": "p1", "name": "Absolute", "brand_name": "Storm"}])
    app.fetch_products_to_search(conn, {"brand_id": "brand-1"}, max_products=90)

    query, params = conn.cursor().executed[0]
    assert "p.published = true" not in query
    assert "p.status = 'current'" in query
    assert "p.brand_id = %s" in query
    assert params[0] == "brand-1"
    assert "order by p.last_video_discovery_at asc nulls first, p.id asc limit %s" in query


def test_fetch_products_to_search_casts_product_ids_to_uuid_array():
    """Real bug found via live smoke test against the real DB: psycopg2
    sends a plain Python list as an untyped Postgres array, which Postgres
    infers as text[]; products.id is uuid, and 'operator does not exist:
    uuid = text' resulted. The %s::uuid[] cast on the parameter is what
    fixes it -- this test guards against that cast getting dropped again."""
    conn = _FakeConn(products=[])
    app.fetch_products_to_search(conn, {"product_ids": ["5c670ec9-6926-4b71-a0ed-b88aa44f219d"]}, max_products=90)

    query, _ = conn.cursor().executed[0]
    assert "p.id = any(%s::uuid[])" in query


def test_fetch_products_to_search_brand_id_filter():
    conn = _FakeConn(products=[])
    app.fetch_products_to_search(conn, {"brand_id": "brand-1"}, max_products=90)

    query, params = conn.cursor().executed[0]
    assert "p.brand_id = %s" in query
    assert "brand-1" in params


def test_insert_candidates_counts_new_rows():
    conn = _FakeConn()
    videos = [
        {"youtube_video_id": "v1", "title": "t1", "channel_title": "c1",
         "published_at": None, "thumbnail_url": None, "match_confidence": "high"},
        {"youtube_video_id": "v2", "title": "t2", "channel_title": "c2",
         "published_at": None, "thumbnail_url": None, "match_confidence": "low"},
    ]
    inserted = app.insert_candidates(conn, "prod-1", "some query", videos)
    assert inserted == 2
    assert conn.committed is True


def test_insert_candidates_is_idempotent_against_already_known_video():
    known = {("prod-1", "v1")}
    conn = _FakeConn(known_pairs=known)
    videos = [
        {"youtube_video_id": "v1", "title": "t1", "channel_title": "c1",
         "published_at": None, "thumbnail_url": None, "match_confidence": "high"},
    ]
    inserted = app.insert_candidates(conn, "prod-1", "some query", videos)
    assert inserted == 0  # already known -- ON CONFLICT DO NOTHING


def test_insert_candidates_writes_stats_fields_when_present():
    """Migration 013 columns -- handler() populates these via fetch_video_
    statistics before calling insert_candidates (see its own comment)."""
    conn = _FakeConn()
    fetched_at = "2026-08-13T00:00:00+00:00"
    videos = [
        {"youtube_video_id": "v1", "title": "t1", "channel_title": "c1",
         "published_at": None, "thumbnail_url": None, "match_confidence": "high",
         "view_count": 12345, "like_count": 678, "comment_count": 9,
         "duration_seconds": 253, "description": "Full review.", "stats_fetched_at": fetched_at},
    ]
    app.insert_candidates(conn, "prod-1", "some query", videos)

    query, params = conn.cursor().executed[0]
    assert "view_count" in query and "stats_fetched_at" in query
    assert params[8:] == (12345, 678, 9, 253, "Full review.", fetched_at)


def test_insert_candidates_defaults_stats_fields_to_none_when_absent():
    """Backward compat: a caller (older tests included) that passes a bare
    video dict without stats keys must not KeyError -- see insert_
    candidates' own docstring on why these are read via .get()."""
    conn = _FakeConn()
    videos = [
        {"youtube_video_id": "v1", "title": "t1", "channel_title": "c1",
         "published_at": None, "thumbnail_url": None, "match_confidence": "high"},
    ]
    app.insert_candidates(conn, "prod-1", "some query", videos)

    _, params = conn.cursor().executed[0]
    assert params[8:] == (None, None, None, None, None, None)


# --- mark_product_searched: what actually drives the rotation fix ---

def test_mark_product_searched_writes_and_commits():
    conn = _FakeConn()
    app.mark_product_searched(conn, "prod-1")

    assert conn.cursor().last_marked_searched == "prod-1"
    assert conn.committed is True


# --- select_video_ids_needing_stats_refresh / apply_video_stats /
# refresh_video_stats: the {"refresh_stats": true} job shape, for
# re-pulling view counts on EXISTING candidates, not just new ones. ---

def test_select_video_ids_needing_stats_refresh_orders_stale_first():
    conn = _FakeConn(refresh_rows=[{"id": "pv1", "youtube_video_id": "v1"}])
    rows = app.select_video_ids_needing_stats_refresh(conn, limit=200)

    assert rows == [{"id": "pv1", "youtube_video_id": "v1"}]
    query, params = conn.cursor().executed[0]
    assert "order by stats_fetched_at asc nulls first, id asc limit %s" in query
    assert params == (200,)


def test_apply_video_stats_with_stats_updates_all_fields():
    conn = _FakeConn()
    # duration=120 (2 minutes) -- deliberately well above MIN_VIDEO_
    # DURATION_SECONDS so this test stays about the plain full-field
    # update, not the SHORTS FILTER path (see the dedicated tests below
    # for that).
    stats = {"view_count": 100, "like_count": 10, "comment_count": 2,
              "duration_seconds": 120, "description": "d"}
    app.apply_video_stats(conn, "pv1", stats, "2026-08-13T00:00:00+00:00")

    assert conn.cursor().stats_updates == [
        (100, 10, 2, 120, "d", "2026-08-13T00:00:00+00:00", "pv1"),
    ]
    assert conn.cursor().short_reject_updates == []  # not a Short -- no reject
    assert conn.committed is True


def test_apply_video_stats_empty_stats_only_touches_fetched_at():
    """stats={} (YouTube returned nothing -- deleted/private video) still
    records that a check happened, but must NOT null out any previously-
    known view/like/comment/duration/description values -- see apply_
    video_stats' own docstring."""
    conn = _FakeConn()
    app.apply_video_stats(conn, "pv1", {}, "2026-08-13T00:00:00+00:00")

    assert conn.cursor().stats_updates == []  # the full-field UPDATE never ran
    assert conn.cursor().stats_fetched_at_only_updates == ["pv1"]
    assert conn.cursor().short_reject_updates == []
    assert conn.committed is True


# --- apply_video_stats' SHORTS FILTER enforcement (refresh time): Al's
# explicit "auto-reject those too" choice -- a row found to be a Short on
# refresh gets force-rejected regardless of its current status, guarded
# against clobbering an existing human rejection. ---

def test_apply_video_stats_force_rejects_a_confirmed_short():
    conn = _FakeConn()
    stats = {"view_count": 50000, "like_count": 1000, "comment_count": 20,
              "duration_seconds": 30, "description": "a short clip"}
    fetched_at = "2026-08-13T00:00:00+00:00"
    app.apply_video_stats(conn, "pv1", stats, fetched_at)

    # Stats still get written (view counts etc. stay accurate even for a
    # video that's about to be rejected).
    assert conn.cursor().stats_updates == [(50000, 1000, 20, 30, "a short clip", fetched_at, "pv1")]
    assert conn.cursor().short_reject_updates == [(fetched_at, app.SHORT_REJECTED_BY, "pv1")]


def test_apply_video_stats_does_not_reject_when_duration_meets_the_minimum():
    conn = _FakeConn()
    stats = {"view_count": 100, "duration_seconds": app.MIN_VIDEO_DURATION_SECONDS}
    app.apply_video_stats(conn, "pv1", stats, "2026-08-13T00:00:00+00:00")

    assert conn.cursor().short_reject_updates == []


def test_apply_video_stats_reject_query_guards_against_clobbering_existing_rejection():
    """SQL-shape check: the reject UPDATE must exclude rows already
    status='rejected' so a human's own resolved_at/resolved_by (a real
    rejection reason) is never silently overwritten with the automated
    one."""
    conn = _FakeConn()
    stats = {"duration_seconds": 10}
    app.apply_video_stats(conn, "pv1", stats, "2026-08-13T00:00:00+00:00")

    reject_queries = [q for q, _ in conn.cursor().executed if "status = 'rejected'" in q]
    assert len(reject_queries) == 1
    assert "and status <> 'rejected'" in reject_queries[0]


def test_refresh_video_stats_updates_found_rows_and_marks_missing_ones_checked():
    conn = _FakeConn(refresh_rows=[
        {"id": "pv1", "youtube_video_id": "v1"},
        {"id": "pv2", "youtube_video_id": "v2-deleted"},
    ])
    videos_list_response = {
        "items": [
            # PT2M (120s), well above MIN_VIDEO_DURATION_SECONDS -- this
            # test is about the found/missing distinction, not the
            # SHORTS FILTER (see the dedicated test below for that).
            {"id": "v1", "statistics": {"viewCount": "100"}, "contentDetails": {"duration": "PT2M"}, "snippet": {}},
            # v2-deleted absent -- simulates a deleted/private video
        ],
    }
    fake = _FakeSession(_FakeResponse(200, json.dumps(videos_list_response), ok=True))

    result = app.refresh_video_stats(conn, "fake-key", limit=200, session=fake)

    assert result == {"candidates_checked": 2, "candidates_updated": 1, "candidates_rejected_as_shorts": 0}
    assert conn.cursor().stats_updates == [(100, None, None, 120, None, conn.cursor().stats_updates[0][5], "pv1")]
    assert conn.cursor().stats_fetched_at_only_updates == ["pv2"]


def test_refresh_video_stats_no_rows_skips_the_api_call_entirely():
    conn = _FakeConn(refresh_rows=[])

    class _ExplodingSession:
        def get(self, *a, **kw):
            raise AssertionError("should never be called -- nothing to refresh")

    result = app.refresh_video_stats(conn, "fake-key", limit=200, session=_ExplodingSession())
    assert result == {"candidates_checked": 0, "candidates_updated": 0, "candidates_rejected_as_shorts": 0}


def test_refresh_video_stats_counts_shorts_rejected_this_run():
    conn = _FakeConn(refresh_rows=[
        {"id": "pv1", "youtube_video_id": "v1-short"},
        {"id": "pv2", "youtube_video_id": "v2-real"},
    ])
    videos_list_response = {
        "items": [
            {"id": "v1-short", "statistics": {"viewCount": "500000"}, "contentDetails": {"duration": "PT30S"}, "snippet": {}},
            {"id": "v2-real", "statistics": {"viewCount": "100"}, "contentDetails": {"duration": "PT5M"}, "snippet": {}},
        ],
    }
    fake = _FakeSession(_FakeResponse(200, json.dumps(videos_list_response), ok=True))

    result = app.refresh_video_stats(conn, "fake-key", limit=200, session=fake)

    assert result == {"candidates_checked": 2, "candidates_updated": 2, "candidates_rejected_as_shorts": 1}
    assert conn.cursor().short_reject_updates == [
        (conn.cursor().short_reject_updates[0][0], app.SHORT_REJECTED_BY, "pv1"),
    ]


# --- handler: search errors must NOT mark a product searched, success
# must -- this is the actual invariant the rotation fix depends on (see
# mark_product_searched's docstring: crediting a quota-exhausted or
# otherwise-failed search as "covered" would push it to the back of the
# line past products never even attempted). No handler test existed
# before this fix; monkeypatch is used here the same way every other test
# file in this project uses it for handler-level wiring checks. ---

def test_handler_marks_only_successfully_searched_products(monkeypatch):
    marked = []
    inserted_for = []

    monkeypatch.setattr(app, "get_youtube_api_key", lambda: "fake-key")
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())
    monkeypatch.setattr(app, "get_youtube_requests_session", lambda: object())
    monkeypatch.setattr(
        app, "fetch_products_to_search",
        lambda conn, job, max_products: [
            {"id": "prod-good", "name": "Absolute", "brand_name": "Storm"},
            {"id": "prod-bad", "name": "Fury", "brand_name": "Brunswick"},
        ],
    )

    def fake_search_youtube(api_key, query, max_results, session=None):
        if "Fury" in query:
            raise RuntimeError("simulated quotaExceeded")
        return [{"youtube_video_id": "v1", "title": "Storm Absolute Review",
                  "channel_title": "c1", "published_at": None, "thumbnail_url": None}]

    monkeypatch.setattr(app, "search_youtube", fake_search_youtube)
    monkeypatch.setattr(app, "insert_candidates", lambda conn, pid, q, videos: inserted_for.append(pid) or len(videos))
    monkeypatch.setattr(app, "mark_product_searched", lambda conn, pid: marked.append(pid))

    result = app.handler({}, None)
    body = json.loads(result["body"])

    assert marked == ["prod-good"]  # NOT prod-bad -- its search raised
    assert inserted_for == ["prod-good"]
    assert body == {
        "products_searched": 2, "new_candidates": 1, "search_errors": 1,
        "circuit_breaker_tripped": False, "products_skipped": 0,
    }


# --- handler's stats enrichment: new candidates get view/like/comment/
# duration/description merged in via fetch_video_statistics right after
# search_youtube, wrapped in its own try/except so a failure there can't
# block the candidate from being saved (see handler's own comment). ---

def test_handler_enriches_new_candidates_with_video_stats(monkeypatch):
    captured_videos = []

    monkeypatch.setattr(app, "get_youtube_api_key", lambda: "fake-key")
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())
    monkeypatch.setattr(app, "get_youtube_requests_session", lambda: object())
    monkeypatch.setattr(
        app, "fetch_products_to_search",
        lambda conn, job, max_products: [{"id": "prod-good", "name": "Absolute", "brand_name": "Storm"}],
    )
    monkeypatch.setattr(
        app, "search_youtube",
        lambda api_key, query, max_results, session=None: [
            {"youtube_video_id": "v1", "title": "Storm Absolute Review",
             "channel_title": "c1", "published_at": None, "thumbnail_url": None},
        ],
    )
    monkeypatch.setattr(
        app, "fetch_video_statistics",
        lambda api_key, video_ids, session=None: {"v1": {"view_count": 999, "like_count": 50,
                                                            "comment_count": 3, "duration_seconds": 120,
                                                            "description": "d"}},
    )

    def fake_insert(conn, pid, q, videos):
        captured_videos.extend(videos)
        return len(videos)

    monkeypatch.setattr(app, "insert_candidates", fake_insert)
    monkeypatch.setattr(app, "mark_product_searched", lambda conn, pid: None)

    app.handler({}, None)

    assert len(captured_videos) == 1
    assert captured_videos[0]["view_count"] == 999
    assert captured_videos[0]["like_count"] == 50
    assert captured_videos[0]["stats_fetched_at"] is not None


def test_handler_stats_enrichment_failure_does_not_block_candidate_insertion(monkeypatch):
    """Al asked for more data points, not for a video's absence of stats
    to become a reason it never gets saved -- same 'secondary data must
    not block the primary write' stance transcript_note already takes."""
    captured_videos = []

    monkeypatch.setattr(app, "get_youtube_api_key", lambda: "fake-key")
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())
    monkeypatch.setattr(app, "get_youtube_requests_session", lambda: object())
    monkeypatch.setattr(
        app, "fetch_products_to_search",
        lambda conn, job, max_products: [{"id": "prod-good", "name": "Absolute", "brand_name": "Storm"}],
    )
    monkeypatch.setattr(
        app, "search_youtube",
        lambda api_key, query, max_results, session=None: [
            {"youtube_video_id": "v1", "title": "Storm Absolute Review",
             "channel_title": "c1", "published_at": None, "thumbnail_url": None},
        ],
    )

    def raising_fetch_video_statistics(api_key, video_ids, session=None):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(app, "fetch_video_statistics", raising_fetch_video_statistics)

    def fake_insert(conn, pid, q, videos):
        captured_videos.extend(videos)
        return len(videos)

    monkeypatch.setattr(app, "insert_candidates", fake_insert)
    marked = []
    monkeypatch.setattr(app, "mark_product_searched", lambda conn, pid: marked.append(pid))

    result = app.handler({}, None)
    body = json.loads(result["body"])

    assert marked == ["prod-good"]  # still marked searched
    assert len(captured_videos) == 1  # still saved, just without stats
    assert captured_videos[0].get("view_count") is None
    assert body["new_candidates"] == 1


# --- handler's SHORTS FILTER enforcement (discovery time): a confirmed
# Short must never reach insert_candidates, but a candidate whose
# duration is still unknown (stats enrichment failed) must still be
# saved -- same distinction as is_likely_short's own docstring. ---

def test_handler_drops_confirmed_shorts_before_insert(monkeypatch):
    captured_videos = []

    monkeypatch.setattr(app, "get_youtube_api_key", lambda: "fake-key")
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())
    monkeypatch.setattr(app, "get_youtube_requests_session", lambda: object())
    monkeypatch.setattr(
        app, "fetch_products_to_search",
        lambda conn, job, max_products: [{"id": "prod-good", "name": "Absolute", "brand_name": "Storm"}],
    )
    monkeypatch.setattr(
        app, "search_youtube",
        lambda api_key, query, max_results, session=None: [
            {"youtube_video_id": "v-short", "title": "Storm Absolute quick look",
             "channel_title": "c1", "published_at": None, "thumbnail_url": None},
            {"youtube_video_id": "v-real", "title": "Storm Absolute Full Review",
             "channel_title": "c1", "published_at": None, "thumbnail_url": None},
        ],
    )
    monkeypatch.setattr(
        app, "fetch_video_statistics",
        lambda api_key, video_ids, session=None: {
            "v-short": {"view_count": 900000, "duration_seconds": 25},  # a Short -- must be dropped
            "v-real": {"view_count": 500, "duration_seconds": 300},     # a real review -- must be kept
        },
    )

    def fake_insert(conn, pid, q, videos):
        captured_videos.extend(videos)
        return len(videos)

    monkeypatch.setattr(app, "insert_candidates", fake_insert)
    monkeypatch.setattr(app, "mark_product_searched", lambda conn, pid: None)

    result = app.handler({}, None)
    body = json.loads(result["body"])

    assert [v["youtube_video_id"] for v in captured_videos] == ["v-real"]
    assert body["new_candidates"] == 1


def test_handler_keeps_candidates_whose_duration_is_unknown(monkeypatch):
    """A confirmed Short gets dropped, but a candidate whose stats
    enrichment simply never returned anything for it (partial videos.list
    response, not a hard failure) must still be saved -- unknown duration
    is never treated as 'must be a Short'."""
    captured_videos = []

    monkeypatch.setattr(app, "get_youtube_api_key", lambda: "fake-key")
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())
    monkeypatch.setattr(app, "get_youtube_requests_session", lambda: object())
    monkeypatch.setattr(
        app, "fetch_products_to_search",
        lambda conn, job, max_products: [{"id": "prod-good", "name": "Absolute", "brand_name": "Storm"}],
    )
    monkeypatch.setattr(
        app, "search_youtube",
        lambda api_key, query, max_results, session=None: [
            {"youtube_video_id": "v-unknown", "title": "Storm Absolute Review",
             "channel_title": "c1", "published_at": None, "thumbnail_url": None},
        ],
    )
    # v-unknown absent from the enrichment result entirely -- duration_
    # seconds is never set on the video dict at all.
    monkeypatch.setattr(app, "fetch_video_statistics", lambda api_key, video_ids, session=None: {})

    def fake_insert(conn, pid, q, videos):
        captured_videos.extend(videos)
        return len(videos)

    monkeypatch.setattr(app, "insert_candidates", fake_insert)
    monkeypatch.setattr(app, "mark_product_searched", lambda conn, pid: None)

    app.handler({}, None)

    assert [v["youtube_video_id"] for v in captured_videos] == ["v-unknown"]


# --- handler's {"refresh_stats": true} job shape: a completely different
# code path from the search flow above -- no products, no circuit
# breaker, just delegates straight to refresh_video_stats. ---

def test_handler_refresh_stats_job_delegates_and_skips_search_flow(monkeypatch):
    monkeypatch.setattr(app, "get_youtube_api_key", lambda: "fake-key")
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())
    monkeypatch.setattr(app, "get_youtube_requests_session", lambda: object())

    def exploding_fetch_products(conn, job, max_products):
        raise AssertionError("fetch_products_to_search must not run for a refresh_stats job")

    monkeypatch.setattr(app, "fetch_products_to_search", exploding_fetch_products)

    captured = {}

    def fake_refresh(conn, api_key, limit=app.DEFAULT_REFRESH_STATS_LIMIT, session=None):
        captured["limit"] = limit
        return {"candidates_checked": 5, "candidates_updated": 3}

    monkeypatch.setattr(app, "refresh_video_stats", fake_refresh)

    result = app.handler({"refresh_stats": True, "limit": 50}, None)
    body = json.loads(result["body"])

    assert body == {"candidates_checked": 5, "candidates_updated": 3}
    assert captured["limit"] == 50


def test_handler_refresh_stats_job_falls_back_to_default_limit(monkeypatch):
    monkeypatch.setattr(app, "get_youtube_api_key", lambda: "fake-key")
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())
    monkeypatch.setattr(app, "get_youtube_requests_session", lambda: object())

    captured = {}

    def fake_refresh(conn, api_key, limit=None, session=None):
        captured["limit"] = limit
        return {"candidates_checked": 0, "candidates_updated": 0}

    monkeypatch.setattr(app, "refresh_video_stats", fake_refresh)

    app.handler({"refresh_stats": True}, None)

    assert captured["limit"] == app.DEFAULT_REFRESH_STATS_LIMIT


# --- Circuit breaker: real incident #2 (see module docstring) -- a
# sustained-throttled batch blew past even the raised 280s Timeout with a
# hard Sandbox.Timedout kill and zero visibility. 5 consecutive 429s now
# stops the loop early instead of paying full retry backoff on every
# remaining product. ---

class _FakeHTTPError(Exception):
    """Duck-typed stand-in for requests.exceptions.HTTPError -- only
    needs a `.response.status_code`, which is all _is_rate_limit_error
    actually looks at (see that function's docstring for why it's
    duck-typed rather than isinstance-checking the real requests
    exception class)."""
    def __init__(self, status_code):
        super().__init__(f"{status_code} error")
        self.response = type("Resp", (), {"status_code": status_code})()


def test_is_rate_limit_error_true_only_for_429():
    assert app._is_rate_limit_error(_FakeHTTPError(429)) is True
    assert app._is_rate_limit_error(_FakeHTTPError(500)) is False
    assert app._is_rate_limit_error(RuntimeError("no response attr at all")) is False


def test_handler_circuit_breaker_stops_after_consecutive_429s(monkeypatch):
    """10 products queued, every search_youtube call raises a 429 --
    should stop after CIRCUIT_BREAKER_THRESHOLD (5) attempts, not grind
    through all 10 paying backoff each time."""
    products = [{"id": f"p{i}", "name": f"Ball {i}", "brand_name": "Storm"} for i in range(10)]
    attempted = []

    monkeypatch.setattr(app, "get_youtube_api_key", lambda: "fake-key")
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())
    monkeypatch.setattr(app, "get_youtube_requests_session", lambda: object())
    monkeypatch.setattr(app, "fetch_products_to_search", lambda conn, job, max_products: products)

    def always_429(api_key, query, max_results, session=None):
        attempted.append(query)
        raise _FakeHTTPError(429)

    monkeypatch.setattr(app, "search_youtube", always_429)
    monkeypatch.setattr(app, "insert_candidates", lambda conn, pid, q, videos: 0)
    monkeypatch.setattr(app, "mark_product_searched", lambda conn, pid: None)

    result = app.handler({}, None)
    body = json.loads(result["body"])

    assert len(attempted) == app.DEFAULT_CIRCUIT_BREAKER_THRESHOLD  # stopped at 5, not all 10
    assert body["circuit_breaker_tripped"] is True
    assert body["search_errors"] == 5
    assert body["products_skipped"] == 5  # the other 5 never attempted at all


def test_handler_circuit_breaker_resets_on_success_between_failures(monkeypatch):
    """4 consecutive 429s, then a success, then 4 more 429s -- should NOT
    trip (never actually hits 5 in a row) even though there are 8 total
    failures across the whole run."""
    products = [{"id": f"p{i}", "name": f"Ball {i}", "brand_name": "Storm"} for i in range(9)]
    attempted = []

    monkeypatch.setattr(app, "get_youtube_api_key", lambda: "fake-key")
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())
    monkeypatch.setattr(app, "get_youtube_requests_session", lambda: object())
    monkeypatch.setattr(app, "fetch_products_to_search", lambda conn, job, max_products: products)

    def mostly_429_one_success(api_key, query, max_results, session=None):
        attempted.append(query)
        if len(attempted) == 5:  # the 5th call (index 4) succeeds
            return []
        raise _FakeHTTPError(429)

    monkeypatch.setattr(app, "search_youtube", mostly_429_one_success)
    monkeypatch.setattr(app, "insert_candidates", lambda conn, pid, q, videos: 0)
    monkeypatch.setattr(app, "mark_product_searched", lambda conn, pid: None)

    result = app.handler({}, None)
    body = json.loads(result["body"])

    assert len(attempted) == 9  # ran the full batch -- breaker never actually tripped
    assert body["circuit_breaker_tripped"] is False
    assert body["search_errors"] == 8
    assert body["products_skipped"] == 0


def test_handler_circuit_breaker_ignores_non_rate_limit_errors(monkeypatch):
    """5 consecutive failures that are NOT 429s (e.g. a real bug) should
    NOT trip the rate-limit circuit breaker -- see _is_rate_limit_error's
    docstring for why only 429s count."""
    products = [{"id": f"p{i}", "name": f"Ball {i}", "brand_name": "Storm"} for i in range(6)]
    attempted = []

    monkeypatch.setattr(app, "get_youtube_api_key", lambda: "fake-key")
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())
    monkeypatch.setattr(app, "get_youtube_requests_session", lambda: object())
    monkeypatch.setattr(app, "fetch_products_to_search", lambda conn, job, max_products: products)

    def always_bug(api_key, query, max_results, session=None):
        attempted.append(query)
        raise RuntimeError("some unrelated real bug")

    monkeypatch.setattr(app, "search_youtube", always_bug)
    monkeypatch.setattr(app, "insert_candidates", lambda conn, pid, q, videos: 0)
    monkeypatch.setattr(app, "mark_product_searched", lambda conn, pid: None)

    result = app.handler({}, None)
    body = json.loads(result["body"])

    assert len(attempted) == 6  # ran the full batch, never tripped
    assert body["circuit_breaker_tripped"] is False
    assert body["search_errors"] == 6


if __name__ == "__main__":
    class _MonkeyPatch:
        def __init__(self):
            self._sets = []

        def setattr(self, obj, name, value):
            self._sets.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, value in reversed(self._sets):
                setattr(obj, name, value)

    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for name, t in tests:
        mp = _MonkeyPatch()
        try:
            if "monkeypatch" in t.__code__.co_varnames[: t.__code__.co_argcount]:
                t(mp)
            else:
                t()
            print(f"PASS: {name}")
            passed += 1
        finally:
            mp.undo()
    print(f"\n{passed}/{len(tests)} tests passed")
