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


def test_build_article_body_html_only_embeds_action_shot_not_product_shot():
    """Al's own call: only the 16:9 action shot belongs in the body --
    the 1:1 product shot's job is now the WebDAV thumbnail, not a second
    body image."""
    article = {
        "title": "T",
        "action_shot_image_url": "https://bucket.s3.amazonaws.com/action.png",
        "product_shot_image_url": "https://bucket.s3.amazonaws.com/product.png",
    }
    body = app.build_article_body_html(article)
    assert body.count("<img") == 1
    assert "product.png" not in body


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


def test_build_bigcommerce_blog_post_payload_includes_thumbnail_path_when_given():
    payload = app.build_bigcommerce_blog_post_payload({"title": "T"}, thumbnail_path="uploaded_images/article-art-1.png")
    assert payload["thumbnail_path"] == "uploaded_images/article-art-1.png"


def test_build_bigcommerce_blog_post_payload_omits_thumbnail_path_when_not_given():
    """No WebDAV configured / upload failed -- must still be a valid,
    publishable payload with no thumbnail_path key at all (not a None or
    empty-string value)."""
    payload = app.build_bigcommerce_blog_post_payload({"title": "T"})
    assert "thumbnail_path" not in payload


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
    def __init__(self, status_code=200, json_body=None, content=b""):
        self.status_code = status_code
        self._json_body = json_body or {}
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._json_body


class _FakeSession:
    def __init__(self, get_response=None, post_response=None, get_exc=None, post_exc=None,
                 get_responses=None, put_response=None, put_exc=None):
        self.get_response = get_response
        # When given, .get() pops responses off this list in call order instead
        # of always returning the single get_response -- needed once one test
        # needs the product-URL lookup and the thumbnail image download (both
        # plain session.get calls) to return two different things.
        self.get_responses = list(get_responses) if get_responses is not None else None
        self.post_response = post_response
        self.put_response = put_response if put_response is not None else _FakeResponse(200)
        self.get_exc = get_exc
        self.post_exc = post_exc
        self.put_exc = put_exc
        self.get_calls = []
        self.post_calls = []
        self.put_calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.get_calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        if self.get_exc is not None:
            raise self.get_exc
        if self.get_responses is not None:
            return self.get_responses.pop(0)
        return self.get_response

    def post(self, url, headers=None, json=None, timeout=None):
        self.post_calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self.post_exc is not None:
            raise self.post_exc
        return self.post_response

    def put(self, url, data=None, auth=None, timeout=None):
        self.put_calls.append({"url": url, "data": data, "auth": auth, "timeout": timeout})
        if self.put_exc is not None:
            raise self.put_exc
        return self.put_response


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


# --- upload_thumbnail_via_webdav: fake requests-Session-shaped object ---

_WEBDAV = {
    "url": "https://store-abc.mybigcommerce.com/dav",
    "username": "wd@example.com",
    "password": "wd-secret",
}


def test_upload_thumbnail_via_webdav_returns_none_when_webdav_not_configured():
    session = _FakeSession()
    result = app.upload_thumbnail_via_webdav(session, None, "art-1", "https://bucket.s3.amazonaws.com/action.png")
    assert result is None
    assert session.get_calls == []


def test_upload_thumbnail_via_webdav_returns_none_when_no_image_url():
    session = _FakeSession()
    result = app.upload_thumbnail_via_webdav(session, _WEBDAV, "art-1", None)
    assert result is None
    assert session.get_calls == []


def test_upload_thumbnail_via_webdav_uploads_and_returns_relative_path():
    session = _FakeSession(get_response=_FakeResponse(200, content=b"fake-png-bytes"))

    result = app.upload_thumbnail_via_webdav(session, _WEBDAV, "art-1", "https://bucket.s3.amazonaws.com/action.png")

    assert result == "uploaded_images/article-art-1.png"
    put_call = session.put_calls[0]
    assert put_call["url"] == "https://store-abc.mybigcommerce.com/dav/product_images/uploaded_images/article-art-1.png"
    assert put_call["data"] == b"fake-png-bytes"


def test_upload_thumbnail_via_webdav_uses_digest_auth_not_basic():
    """REAL INCIDENT (2026-09-05, Al's first live test): BigCommerce's
    WebDAV is backed by SabreDAV, which rejects plain HTTP Basic Auth --
    a live curl -v reproduction with correct, Cyberduck-verified-working
    credentials got back 401 with `WWW-Authenticate: Digest
    realm="SabreDAV"...` and SabreDAV's own "No 'Authorization: Digest'
    header found" error body. Cyberduck performs that handshake
    transparently, which is why "Cyberduck connects fine" and "the
    Lambda gets 401" were both true -- never a credentials problem.
    Pins that the fix (requests.auth.HTTPDigestAuth, not a plain
    (user, pass) tuple which requests treats as Basic Auth) stays in
    place."""
    import requests

    session = _FakeSession(get_response=_FakeResponse(200, content=b"fake-png-bytes"))
    app.upload_thumbnail_via_webdav(session, _WEBDAV, "art-1", "https://bucket.s3.amazonaws.com/action.png")

    auth = session.put_calls[0]["auth"]
    assert isinstance(auth, requests.auth.HTTPDigestAuth)
    assert auth.username == "wd@example.com"
    assert auth.password == "wd-secret"


def test_upload_thumbnail_via_webdav_strips_trailing_slash_from_webdav_url():
    webdav = {**_WEBDAV, "url": "https://store-abc.mybigcommerce.com/dav/"}
    session = _FakeSession(get_response=_FakeResponse(200, content=b"bytes"))
    app.upload_thumbnail_via_webdav(session, webdav, "art-1", "https://bucket.s3.amazonaws.com/action.png")
    assert "//product_images" not in session.put_calls[0]["url"]


def test_upload_thumbnail_via_webdav_defaults_unrecognized_extension_to_png():
    session = _FakeSession(get_response=_FakeResponse(200, content=b"bytes"))
    result = app.upload_thumbnail_via_webdav(session, _WEBDAV, "art-1", "https://bucket.s3.amazonaws.com/action")
    assert result == "uploaded_images/article-art-1.png"


def test_upload_thumbnail_via_webdav_returns_none_on_download_failure():
    session = _FakeSession(get_exc=RuntimeError("download boom"))
    result = app.upload_thumbnail_via_webdav(session, _WEBDAV, "art-1", "https://bucket.s3.amazonaws.com/action.png")
    assert result is None


def test_upload_thumbnail_via_webdav_returns_none_on_upload_exception():
    session = _FakeSession(get_response=_FakeResponse(200, content=b"bytes"), put_exc=RuntimeError("upload boom"))
    result = app.upload_thumbnail_via_webdav(session, _WEBDAV, "art-1", "https://bucket.s3.amazonaws.com/action.png")
    assert result is None


def test_upload_thumbnail_via_webdav_returns_none_on_upload_http_error():
    session = _FakeSession(get_response=_FakeResponse(200, content=b"bytes"), put_response=_FakeResponse(403))
    result = app.upload_thumbnail_via_webdav(session, _WEBDAV, "art-1", "https://bucket.s3.amazonaws.com/action.png")
    assert result is None


# --- get_bigcommerce_credentials: sys.modules-injected fake boto3 (not installed here) ---

def _with_fake_boto3(secret_dict, fn):
    """boto3 isn't actually installed in this sandbox (pip's proxy 403s
    here), so this injects a fake module into sys.modules the same way
    test_product_article_generator.py's _HandlerPatchGuard does for its
    own bare `import boto3` calls -- saves/restores both sys.modules["boto3"]
    and BIGCOMMERCE_SECRET_ARN so this can't leak into any other test."""
    import types

    class _FakeSecretsManagerClient:
        def get_secret_value(self, SecretId):
            return {"SecretString": json.dumps(secret_dict)}

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda service_name, **kwargs: _FakeSecretsManagerClient()

    old_boto3 = sys.modules.get("boto3")
    old_env = os.environ.get("BIGCOMMERCE_SECRET_ARN")
    sys.modules["boto3"] = fake_boto3
    os.environ["BIGCOMMERCE_SECRET_ARN"] = "arn:aws:secretsmanager:us-west-1:123:secret:bigcommerce"
    try:
        return fn()
    finally:
        if old_boto3 is not None:
            sys.modules["boto3"] = old_boto3
        else:
            del sys.modules["boto3"]
        if old_env is not None:
            os.environ["BIGCOMMERCE_SECRET_ARN"] = old_env
        else:
            del os.environ["BIGCOMMERCE_SECRET_ARN"]


def test_get_bigcommerce_credentials_returns_webdav_none_when_keys_missing():
    store_hash, auth_token, webdav = _with_fake_boto3(
        {"store_hash": "abc", "auth_token": "tok"}, app.get_bigcommerce_credentials,
    )
    assert store_hash == "abc"
    assert auth_token == "tok"
    assert webdav is None


def test_get_bigcommerce_credentials_returns_webdav_none_when_only_some_keys_present():
    store_hash, auth_token, webdav = _with_fake_boto3(
        {"store_hash": "abc", "auth_token": "tok", "webdav_url": "https://x/dav"},
        app.get_bigcommerce_credentials,
    )
    assert webdav is None


def test_get_bigcommerce_credentials_returns_webdav_dict_when_all_keys_present():
    store_hash, auth_token, webdav = _with_fake_boto3(
        {
            "store_hash": "abc", "auth_token": "tok",
            "webdav_url": "https://store-abc.mybigcommerce.com/dav/",
            "webdav_username": "wd@example.com", "webdav_password": "wd-secret",
        },
        app.get_bigcommerce_credentials,
    )
    assert webdav == {
        "url": "https://store-abc.mybigcommerce.com/dav",
        "username": "wd@example.com",
        "password": "wd-secret",
    }


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


def test_process_one_article_skips_thumbnail_when_webdav_not_configured():
    """Default/no-webdav call shape (also what every other _process_one_
    article test above already exercises) -- must never attempt a PUT and
    must not add a thumbnail_path key to the outgoing payload."""
    conn = _FakeConnection()
    session = _FakeSession(
        get_response=_FakeResponse(200, {"data": {"custom_url": {"url": "/x/"}}}),
        post_response=_FakeResponse(200, {"id": 777}),
    )
    article = {
        "article_id": "art-1", "product_id": "prod-1", "bigcommerce_product_id": "4390", "title": "T",
        "action_shot_image_url": "https://bucket.s3.amazonaws.com/action.png",
    }

    result = app._process_one_article(session, conn, "store-hash-1", "token-123", article)

    assert result["success"] is True
    assert session.put_calls == []
    assert "thumbnail_path" not in session.post_calls[0]["json"]


def test_process_one_article_uploads_thumbnail_and_sets_thumbnail_path_when_webdav_configured():
    conn = _FakeConnection()
    session = _FakeSession(
        get_responses=[
            _FakeResponse(200, {"data": {"custom_url": {"url": "/x/"}}}),  # product URL lookup
            _FakeResponse(200, content=b"fake-png-bytes"),  # thumbnail image download
        ],
        post_response=_FakeResponse(200, {"id": 777}),
    )
    article = {
        "article_id": "art-1", "product_id": "prod-1", "bigcommerce_product_id": "4390", "title": "T",
        "action_shot_image_url": "https://bucket.s3.amazonaws.com/action.png",
    }

    result = app._process_one_article(session, conn, "store-hash-1", "token-123", article, webdav=_WEBDAV)

    assert result["success"] is True
    assert session.put_calls[0]["url"].endswith("/product_images/uploaded_images/article-art-1.png")
    assert session.post_calls[0]["json"]["thumbnail_path"] == "uploaded_images/article-art-1.png"


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
    monkeypatch.setattr(app, "get_bigcommerce_credentials", lambda: ("store-hash-1", "token-123", None))

    def _fake_process(session, conn_, store_hash, auth_token, article, webdav=None):
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
