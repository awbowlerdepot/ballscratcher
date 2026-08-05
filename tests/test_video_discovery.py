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
        self._response = response
        self.get_calls = []

    def get(self, url, params=None, timeout=None):
        self.get_calls.append({"url": url, "params": params})
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


# --- Fake DB layer: fetch_products_to_search / insert_candidates ---

class _FakeCursor:
    """Mimics enough of psycopg2's cursor to exercise the two DB-facing
    functions below: fetchall()+description for fetch_products_to_search's
    select, and a rowcount-based on-conflict-do-nothing simulation for
    insert_candidates."""

    def __init__(self, products=None, known_pairs=None):
        self.products = products or []
        self.known_pairs = known_pairs or set()
        self.executed = []
        self.description = None
        self.rowcount = 0
        self._rows = []
        self.last_marked_searched = None

    def execute(self, query, params=None):
        params = params or []
        q = " ".join(query.split())
        self.executed.append((q, params))

        if q.startswith("select p.id, p.name, b.name as brand_name"):
            self.description = [("id",), ("name",), ("brand_name",)]
            self._rows = [(p["id"], p["name"], p["brand_name"]) for p in self.products]
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
        else:
            raise NotImplementedError(f"FakeCursor doesn't support: {q}")

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, products=None, known_pairs=None):
        self._cursor = _FakeCursor(products, known_pairs)
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


# --- mark_product_searched: what actually drives the rotation fix ---

def test_mark_product_searched_writes_and_commits():
    conn = _FakeConn()
    app.mark_product_searched(conn, "prod-1")

    assert conn.cursor().last_marked_searched == "prod-1"
    assert conn.committed is True


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
    assert body == {"products_searched": 2, "new_candidates": 1, "search_errors": 1}


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
