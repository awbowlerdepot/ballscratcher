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
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "video_discovery"))

import app  # noqa: E402


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

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


def test_fetch_products_to_search_defaults_to_published_current():
    conn = _FakeConn(products=[{"id": "p1", "name": "Absolute", "brand_name": "Storm"}])
    products = app.fetch_products_to_search(conn, {}, max_products=90)

    assert products == [{"id": "p1", "name": "Absolute", "brand_name": "Storm"}]
    query, params = conn.cursor().executed[0]
    assert "p.published = true" in query
    assert "p.status = 'current'" in query
    assert params[-1] == 90  # the limit


def test_fetch_products_to_search_explicit_product_ids_skips_published_filter():
    conn = _FakeConn(products=[{"id": "p1", "name": "Absolute", "brand_name": "Storm"}])
    app.fetch_products_to_search(conn, {"product_ids": ["p1"]}, max_products=90)

    query, params = conn.cursor().executed[0]
    assert "p.id = any(" in query
    assert "p.published = true" not in query
    assert params[0] == ["p1"]


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


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS: {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")
