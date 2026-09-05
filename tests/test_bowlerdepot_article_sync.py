"""
Tests for src/bowlerdepot_article_sync/app.py -- the server-side push of
approved, admin-flagged ball-review articles into BigCommerce's native
Blog as new posts (see app.py's own module docstring for the full "why",
including the real Al-flipped-the-flag-and-nothing-happened incident that
led to this module actually getting built).

Manual-runner pattern, run standalone via
`python3 tests/test_bowlerdepot_article_sync.py` -- same convention as
test_bowlerdepot_video_sync.py (no pytest in this sandbox).

Everything DB-facing is exercised against a fake psycopg2-shaped
cursor/connection; the BigCommerce calls are exercised against a fake
requests-Session-shaped object (no real HTTP call, no live BigCommerce
credentials available in this sandbox).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "bowlerdepot_article_sync"))

import app  # noqa: E402


# --- build_article_body_html / build_bigcommerce_blog_post_payload: pure ---

def test_build_article_body_html_includes_all_sections_and_link():
    article = {
        "title": "The Storm Equinox Solid",
        "hook": "Picture this...",
        "performance_summary": "Strong midlane read.",
        "who_should_buy": ["Heavy oil bowlers"],
        "who_should_skip": ["Light oil bowlers"],
        "pros": ["Strong backend"],
        "cons": ["Not for light oil"],
        "buying_tips": "Drill for control.",
        "verdict": "A solid heavy-oil piece.",
        "faq": [{"question": "How does it hook?", "answer": "A lot."}],
        "action_shot_image_url": "https://bucket.s3.amazonaws.com/action.png",
        "product_shot_image_url": "https://bucket.s3.amazonaws.com/product.png",
    }
    body = app.build_article_body_html(article, product_url="/storm-equinox-solid/")

    assert '<img src="https://bucket.s3.amazonaws.com/action.png"' in body
    assert '<img src="https://bucket.s3.amazonaws.com/product.png"' in body
    assert "<em>Picture this...</em>" in body
    assert "Strong midlane read." in body
    assert "<li>Heavy oil bowlers</li>" in body
    assert "<li>Light oil bowlers</li>" in body
    assert "<li>Strong backend</li>" in body
    assert "<li>Not for light oil</li>" in body
    assert "Drill for control." in body
    assert "A solid heavy-oil piece." in body
    assert "How does it hook?" in body and "A lot." in body
    assert '<a href="/storm-equinox-solid/">View this ball on BowlerDepot</a>' in body


def test_build_article_body_html_omits_link_when_product_url_is_none():
    body = app.build_article_body_html({"title": "T"}, product_url=None)
    assert "View this ball" not in body


def test_build_article_body_html_escapes_html_special_characters():
    article = {"title": "T", "hook": "Storm & Roto <Grip>"}
    body = app.build_article_body_html(article)
    assert "Storm &amp; Roto &lt;Grip&gt;" in body
    assert "<Grip>" not in body


def test_build_article_body_html_skips_missing_optional_sections():
    body = app.build_article_body_html({"title": "T"}, product_url=None)
    assert "<img" not in body
    assert "<ul>" not in body


def test_build_bigcommerce_blog_post_payload_maps_fields():
    article = {"title": "The Storm Equinox Solid", "hook": "Hi", "brand_name": "Storm"}
    payload = app.build_bigcommerce_blog_post_payload(article, product_url="/x/")

    assert payload["title"] == "The Storm Equinox Solid"
    assert payload["is_published"] is True
    assert payload["author"] == "BowlerDepot"
    assert payload["tags"] == ["Storm"]
    assert "Hi" in payload["body"]


def test_build_bigcommerce_blog_post_payload_no_brand_name_gives_empty_tags():
    payload = app.build_bigcommerce_blog_post_payload({"title": "T"})
    assert payload["tags"] == []


# --- list_articles_needing_sync: fake psycopg2-shaped cursor/connection ---

class _FakeCursor:
    def __init__(self, articles_needing_sync=None):
        self.articles_needing_sync = articles_needing_sync or []
        self.executed = []
        self.description = None
        self._rows = []
        self.updates = []  # list of (bigcommerce_post_id, article_id) from mark_article_synced

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        params = params or ()
        q = " ".join(query.split())
        self.executed.append((q, params))

        if q.startswith("select pa.id as article_id, pa.product_id, bp.bigcommerce_product_id"):
            self.description = [
                ("article_id",), ("product_id",), ("bigcommerce_product_id",), ("title",), ("hook",),
                ("performance_summary",), ("who_should_buy",), ("who_should_skip",), ("pros",), ("cons",),
                ("buying_tips",), ("verdict",), ("faq",), ("action_shot_image_url",),
                ("product_shot_image_url",), ("brand_name",),
            ]
            rows = self.articles_needing_sync
            if "and pa.id = %s" in q:
                (only_id,) = params
                rows = [a for a in rows if a["article_id"] == only_id]
            self._rows = [
                (
                    a["article_id"], a["product_id"], a["bigcommerce_product_id"], a.get("title"), a.get("hook"),
                    a.get("performance_summary"), a.get("who_should_buy"), a.get("who_should_skip"),
                    a.get("pros"), a.get("cons"), a.get("buying_tips"), a.get("verdict"), a.get("faq"),
                    a.get("action_shot_image_url"), a.get("product_shot_image_url"), a.get("brand_name"),
                )
                for a in rows
            ]

        elif q.startswith("update product_articles set bigcommerce_post_id"):
            self.updates.append(tuple(params))

        else:
            raise NotImplementedError(f"FakeCursor doesn't support: {q}")

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, articles_needing_sync=None):
        self._cursor = _FakeCursor(articles_needing_sync)
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def test_list_articles_needing_sync_maps_rows_to_dicts():
    conn = _FakeConnection(articles_needing_sync=[
        {"article_id": "art-1", "product_id": "prod-1", "bigcommerce_product_id": "4390",
         "title": "T", "brand_name": "Storm"},
    ])
    results = app.list_articles_needing_sync(conn)
    assert len(results) == 1
    assert results[0]["article_id"] == "art-1"
    assert results[0]["brand_name"] == "Storm"


def test_list_articles_needing_sync_empty_when_nothing_pending():
    conn = _FakeConnection(articles_needing_sync=[])
    assert app.list_articles_needing_sync(conn) == []


def test_list_articles_needing_sync_query_requires_approved_flagged_matched():
    """Fake cursor above doesn't simulate real SQL filtering (no Postgres
    in this sandbox) -- this verifies the executed query text actually
    contains every real condition rather than one having been silently
    dropped."""
    conn = _FakeConnection(articles_needing_sync=[])
    app.list_articles_needing_sync(conn)

    executed_query = conn._cursor.executed[0][0]
    assert "pa.status = 'approved'" in executed_query
    assert "pa.sync_to_bigcommerce = true" in executed_query
    assert "pa.bowlerdepot_synced_at is null" in executed_query
    assert "bp.match_status = 'matched'" in executed_query
    assert "and pa.id = %s" not in executed_query


def test_list_articles_needing_sync_scopes_to_one_article_id():
    conn = _FakeConnection(articles_needing_sync=[
        {"article_id": "art-1", "product_id": "prod-1", "bigcommerce_product_id": "4390"},
        {"article_id": "art-2", "product_id": "prod-2", "bigcommerce_product_id": "9001"},
    ])
    results = app.list_articles_needing_sync(conn, article_id="art-2")
    assert [r["article_id"] for r in results] == ["art-2"]

    executed_query = conn._cursor.executed[0][0]
    assert "and pa.id = %s" in executed_query


# --- fetch_bigcommerce_product_url: fake requests-Session-shaped object ---

class _FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._json_body


class _FakeSession:
    def __init__(self, get_response=None, post_response=None, get_exc=None, post_exc=None):
        self.get_response = get_response
        self.post_response = post_response
        self.get_exc = get_exc
        self.post_exc = post_exc
        self.get_calls = []
        self.post_calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.get_calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        if self.get_exc is not None:
            raise self.get_exc
        return self.get_response

    def post(self, url, headers=None, json=None, timeout=None):
        self.post_calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self.post_exc is not None:
            raise self.post_exc
        return self.post_response


def test_fetch_bigcommerce_product_url_returns_custom_url():
    session = _FakeSession(get_response=_FakeResponse(200, {"data": {"custom_url": {"url": "/x/", "is_customized": True}}}))

    result = app.fetch_bigcommerce_product_url(session, "store-hash-1", "token-123", "4390")

    assert result == "/x/"
    call = session.get_calls[0]
    assert call["url"] == "https://api.bigcommerce.com/stores/store-hash-1/v3/catalog/products/4390"
    assert call["headers"]["X-Auth-Token"] == "token-123"


def test_fetch_bigcommerce_product_url_returns_none_on_http_error():
    session = _FakeSession(get_response=_FakeResponse(404, {}))
    assert app.fetch_bigcommerce_product_url(session, "store-hash-1", "token-123", "4390") is None


def test_fetch_bigcommerce_product_url_returns_none_on_network_exception():
    session = _FakeSession(get_exc=RuntimeError("network boom"))
    assert app.fetch_bigcommerce_product_url(session, "store-hash-1", "token-123", "4390") is None


def test_fetch_bigcommerce_product_url_returns_none_when_custom_url_missing():
    session = _FakeSession(get_response=_FakeResponse(200, {"data": {}}))
    assert app.fetch_bigcommerce_product_url(session, "store-hash-1", "token-123", "4390") is None


# --- push_article_to_bigcommerce: v2 API, UNWRAPPED response (no "data" key) ---

def test_push_article_to_bigcommerce_posts_to_v2_endpoint_and_returns_raw_object():
    """The one real shape difference from bowlerdepot_video_sync's push_
    video_to_bigcommerce: Blog Posts (v2) return the created object
    directly, NOT wrapped in a "data" key the way the v3 Catalog Product
    Videos endpoint is. Getting this wrong would store the string "id"
    (a KeyError, actually) or a dict instead of the real post id."""
    session = _FakeSession(post_response=_FakeResponse(200, {"id": 777, "title": "T"}))
    payload = {"title": "T", "body": "<p>B</p>", "is_published": True, "author": "BowlerDepot", "tags": []}

    result = app.push_article_to_bigcommerce(session, "store-hash-1", "token-123", payload)

    assert result == {"id": 777, "title": "T"}
    call = session.post_calls[0]
    assert call["url"] == "https://api.bigcommerce.com/stores/store-hash-1/v2/blog/posts"
    assert call["headers"]["X-Auth-Token"] == "token-123"
    assert call["json"] == payload


def test_push_article_to_bigcommerce_raises_on_error_response():
    """A 403 is the real, plausible first-run outcome this module's own
    docstring flags (the token missing the Content-modify scope) --
    push_article_to_bigcommerce itself must let this propagate; it's
    _process_one_article's job (tested below) to catch it per-article."""
    session = _FakeSession(post_response=_FakeResponse(403, {}))
    try:
        app.push_article_to_bigcommerce(session, "store-hash-1", "token-123", {"title": "T", "body": "B"})
        assert False, "expected an exception"
    except Exception as exc:
        assert "403" in str(exc)


# --- mark_article_synced: fake cursor/connection ---

def test_mark_article_synced_writes_id_and_commits():
    conn = _FakeConnection()
    app.mark_article_synced(conn, "art-1", 777)
    assert conn._cursor.updates == [("777", "art-1")]
    assert conn.commits == 1


# --- _process_one_article: catches exceptions per-article, never propagates ---

def test_process_one_article_success():
    conn = _FakeConnection()
    session = _FakeSession(
        get_response=_FakeResponse(200, {"data": {"custom_url": {"url": "/x/"}}}),
        post_response=_FakeResponse(200, {"id": 777}),
    )
    article = {"article_id": "art-1", "product_id": "prod-1", "bigcommerce_product_id": "4390", "title": "T"}

    result = app._process_one_article(session, conn, "store-hash-1", "token-123", article)

    assert result == {"article_id": "art-1", "success": True, "bigcommerce_post_id": 777}
    assert conn._cursor.updates == [("777", "art-1")]


def test_process_one_article_still_succeeds_when_url_lookup_fails():
    """A failed product-URL lookup must not block the article from being
    posted at all -- see fetch_bigcommerce_product_url's own docstring."""
    conn = _FakeConnection()
    session = _FakeSession(
        get_exc=RuntimeError("url lookup boom"),
        post_response=_FakeResponse(200, {"id": 777}),
    )
    article = {"article_id": "art-1", "product_id": "prod-1", "bigcommerce_product_id": "4390", "title": "T"}

    result = app._process_one_article(session, conn, "store-hash-1", "token-123", article)

    assert result["success"] is True
    assert "View this ball" not in session.post_calls[0]["json"]["body"]


def test_process_one_article_catches_push_failure_and_reports_error():
    conn = _FakeConnection()
    session = _FakeSession(
        get_response=_FakeResponse(200, {"data": {}}),
        post_exc=RuntimeError("boom"),
    )
    article = {"article_id": "art-1", "product_id": "prod-1", "bigcommerce_product_id": "4390", "title": "T"}

    result = app._process_one_article(session, conn, "store-hash-1", "token-123", article)

    assert result == {"article_id": "art-1", "success": False, "error": "boom"}
    # A failed push must never write bigcommerce_post_id/bowlerdepot_synced_at.
    assert conn._cursor.updates == []


# --- handler: orchestration only -- DB/credentials/per-article push all monkeypatched ---

def test_handler_returns_early_with_zero_counts_when_nothing_needs_sync(monkeypatch):
    conn = _FakeConnection()
    monkeypatch.setattr(app, "get_db_connection", lambda: conn)
    monkeypatch.setattr(app, "list_articles_needing_sync", lambda c, article_id=None: [])

    result = app.handler({}, None)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body == {"pushed": 0, "failed": 0, "results": []}
    assert conn.closed is True


def test_handler_passes_article_id_through_from_event(monkeypatch):
    conn = _FakeConnection()
    seen = {}

    def _fake_list(c, article_id=None):
        seen["article_id"] = article_id
        return []

    monkeypatch.setattr(app, "get_db_connection", lambda: conn)
    monkeypatch.setattr(app, "list_articles_needing_sync", _fake_list)

    app.handler({"article_id": "art-42"}, None)

    assert seen["article_id"] == "art-42"


def test_handler_processes_every_article_and_tallies_results(monkeypatch):
    conn = _FakeConnection()
    articles = [
        {"article_id": "art-1", "product_id": "prod-1", "bigcommerce_product_id": "4390", "title": "T1"},
        {"article_id": "art-2", "product_id": "prod-2", "bigcommerce_product_id": "9001", "title": "T2"},
    ]
    monkeypatch.setattr(app, "get_db_connection", lambda: conn)
    monkeypatch.setattr(app, "list_articles_needing_sync", lambda c, article_id=None: articles)
    monkeypatch.setattr(app, "get_bigcommerce_credentials", lambda: ("store-hash-1", "token-123"))

    def _fake_process(session, conn_, store_hash, auth_token, article):
        if article["article_id"] == "art-1":
            return {"article_id": "art-1", "success": False, "error": "boom"}
        return {"article_id": "art-2", "success": True, "bigcommerce_post_id": 1}

    monkeypatch.setattr(app, "_process_one_article", _fake_process)

    result = app.handler({}, None)

    body = json.loads(result["body"])
    assert body["pushed"] == 1
    assert body["failed"] == 1
    assert conn.closed is True


if __name__ == "__main__":
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for name, t in tests:
        import inspect

        if "monkeypatch" in inspect.signature(t).parameters:
            class _MonkeyPatch:
                def __init__(self):
                    self._undo = []

                def setattr(self, obj, name, value):
                    self._undo.append((obj, name, getattr(obj, name)))
                    setattr(obj, name, value)

                def undo(self):
                    for obj, name, old in reversed(self._undo):
                        setattr(obj, name, old)

            mp = _MonkeyPatch()
            try:
                t(mp)
            finally:
                mp.undo()
        else:
            t()
        print(f"PASS: {name}")
        passed += 1

    print(f"\n{passed}/{len(tests)} tests passed")
