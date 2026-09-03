"""
Tests for src/bowlerdepot_video_sync/app.py -- the server-side push of
approved, summarized YouTube videos into BigCommerce's native Product
Videos feature (see app.py's own module docstring for the full "why").

Manual-runner pattern, run standalone via
`python3 tests/test_bowlerdepot_video_sync.py` -- same convention as
test_price_checker.py/test_video_discovery.py (no pytest in this sandbox).

Everything DB-facing is exercised against a fake psycopg2-shaped
cursor/connection; push_video_to_bigcommerce is exercised against a fake
requests-Session-shaped object (no real HTTP call, no live BigCommerce
credentials available in this sandbox). handler's own test monkeypatches
the module-level DB/BigCommerce-credential/per-video-push functions
rather than faking a live requests.Session end-to-end -- same "test the
orchestration logic, not the transport" split test_price_checker.py's own
check_price_source tests use against app.fetch_page.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "bowlerdepot_video_sync"))

import app  # noqa: E402


# --- build_bigcommerce_video_payload: pure, no DB, no network ---

def test_build_bigcommerce_video_payload_maps_fields():
    video = {
        "title": "Brunswick Combat Solid Ball Review",
        "summary": "Reviewers found strong midlane read and good backend motion.",
        "youtube_video_id": "abc12345678",
    }
    payload = app.build_bigcommerce_video_payload(video, sort_order=2)
    assert payload == {
        "title": "Brunswick Combat Solid Ball Review",
        "description": "Reviewers found strong midlane read and good backend motion.",
        "sort_order": 2,
        "type": "youtube",
        "video_id": "abc12345678",
    }


def test_build_bigcommerce_video_payload_truncates_long_title():
    video = {
        "title": "x" * 400,
        "summary": "A summary.",
        "youtube_video_id": "abc12345678",
    }
    payload = app.build_bigcommerce_video_payload(video, sort_order=0)
    assert len(payload["title"]) == app.MAX_TITLE_LENGTH


def test_build_bigcommerce_video_payload_missing_title_and_summary_default_to_empty_strings():
    video = {"title": None, "summary": None, "youtube_video_id": "abc12345678"}
    payload = app.build_bigcommerce_video_payload(video, sort_order=0)
    assert payload["title"] == ""
    assert payload["description"] == ""


# --- list_videos_needing_sync: fake psycopg2-shaped cursor/connection ---

class _FakeCursor:
    def __init__(self, videos_needing_sync=None):
        self.videos_needing_sync = videos_needing_sync or []
        self.executed = []
        self.description = None
        self._rows = []
        self.updates = []  # list of (bowlerdepot_video_id, video_id) from mark_video_synced

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        params = params or ()
        q = " ".join(query.split())
        self.executed.append((q, params))

        if q.startswith("select pv.id as video_id, pv.product_id, bp.bigcommerce_product_id"):
            self.description = [
                ("video_id",), ("product_id",), ("bigcommerce_product_id",), ("youtube_video_id",),
                ("title",), ("summary",), ("already_synced_count",),
            ]
            self._rows = [
                (
                    v["video_id"], v["product_id"], v["bigcommerce_product_id"], v["youtube_video_id"],
                    v.get("title"), v.get("summary"), v.get("already_synced_count", 0),
                )
                for v in self.videos_needing_sync
            ]

        elif q.startswith("update product_videos set bowlerdepot_video_id"):
            self.updates.append(tuple(params))

        else:
            raise NotImplementedError(f"FakeCursor doesn't support: {q}")

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, videos_needing_sync=None):
        self._cursor = _FakeCursor(videos_needing_sync)
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def test_list_videos_needing_sync_maps_rows_to_dicts():
    conn = _FakeConnection(videos_needing_sync=[
        {
            "video_id": "vid-1", "product_id": "prod-1", "bigcommerce_product_id": "4390",
            "youtube_video_id": "abc12345678", "title": "Review", "summary": "Good ball.",
            "already_synced_count": 0,
        },
    ])
    results = app.list_videos_needing_sync(conn)
    assert results == [{
        "video_id": "vid-1", "product_id": "prod-1", "bigcommerce_product_id": "4390",
        "youtube_video_id": "abc12345678", "title": "Review", "summary": "Good ball.",
        "already_synced_count": 0,
    }]


def test_list_videos_needing_sync_empty_when_nothing_pending():
    conn = _FakeConnection(videos_needing_sync=[])
    assert app.list_videos_needing_sync(conn) == []


def test_list_videos_needing_sync_query_excludes_blocked_channels():
    """The fake cursor above doesn't simulate real SQL filtering (no
    Postgres available in this sandbox, same limitation as every other
    DB-touching test file here) -- videos_needing_sync is returned as-is,
    already "post-filter". What this test CAN verify is that the
    executed query text actually contains the blocked_video_channels
    exclusion (021_blocked_video_channels.sql) rather than that filter
    having been silently dropped or never wired up."""
    conn = _FakeConnection(videos_needing_sync=[])
    app.list_videos_needing_sync(conn)

    executed_query = conn._cursor.executed[0][0]
    assert "blocked_video_channels" in executed_query
    assert "not exists" in executed_query
    assert "lower(bvc.channel_title) = lower(pv.channel_title)" in executed_query


# --- push_video_to_bigcommerce: fake requests-Session-shaped object ---

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
    def __init__(self, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


def test_push_video_to_bigcommerce_posts_to_expected_url_with_auth_header():
    session = _FakeSession(response=_FakeResponse(200, {"data": {"id": 555}}))
    payload = {"title": "T", "description": "D", "sort_order": 0, "type": "youtube", "video_id": "abc12345678"}

    result = app.push_video_to_bigcommerce(session, "store-hash-1", "token-123", "4390", payload)

    assert result == {"id": 555}
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://api.bigcommerce.com/stores/store-hash-1/v3/catalog/products/4390/videos"
    assert call["headers"]["X-Auth-Token"] == "token-123"
    assert call["json"] == payload


def test_push_video_to_bigcommerce_raises_on_error_response():
    """A 403 is the real, plausible first-run outcome the module docstring
    flags (read-only token missing the Products "modify" scope) --
    push_video_to_bigcommerce itself must let this propagate; it's
    _process_one_video's job (tested below) to catch it per-video."""
    session = _FakeSession(response=_FakeResponse(403, {}))
    payload = {"title": "T", "description": "D", "sort_order": 0, "type": "youtube", "video_id": "abc12345678"}

    try:
        app.push_video_to_bigcommerce(session, "store-hash-1", "token-123", "4390", payload)
        assert False, "expected an exception"
    except Exception as exc:
        assert "403" in str(exc)


# --- mark_video_synced: fake cursor/connection ---

def test_mark_video_synced_writes_id_and_commits():
    conn = _FakeConnection()
    app.mark_video_synced(conn, "vid-1", 555)
    assert conn._cursor.updates == [("555", "vid-1")]
    assert conn.commits == 1


# --- _process_one_video: catches exceptions per-video, never propagates ---

def test_process_one_video_success():
    conn = _FakeConnection()
    session = _FakeSession(response=_FakeResponse(200, {"data": {"id": 555}}))
    video = {
        "video_id": "vid-1", "product_id": "prod-1", "bigcommerce_product_id": "4390",
        "youtube_video_id": "abc12345678", "title": "Review", "summary": "Good ball.",
    }

    result = app._process_one_video(session, conn, "store-hash-1", "token-123", video, sort_order=0)

    assert result == {"video_id": "vid-1", "success": True}
    assert conn._cursor.updates == [("555", "vid-1")]


def test_process_one_video_catches_push_failure_and_reports_error():
    conn = _FakeConnection()
    session = _FakeSession(raise_exc=RuntimeError("boom"))
    video = {
        "video_id": "vid-1", "product_id": "prod-1", "bigcommerce_product_id": "4390",
        "youtube_video_id": "abc12345678", "title": "Review", "summary": "Good ball.",
    }

    result = app._process_one_video(session, conn, "store-hash-1", "token-123", video, sort_order=0)

    assert result == {"video_id": "vid-1", "success": False, "error": "boom"}
    # A failed push must never write bowlerdepot_video_id/bowlerdepot_synced_at.
    assert conn._cursor.updates == []


# --- handler: orchestration only -- DB/credentials/per-video push all monkeypatched ---

def test_handler_returns_early_with_zero_counts_when_nothing_needs_sync(monkeypatch):
    conn = _FakeConnection()
    monkeypatch.setattr(app, "get_db_connection", lambda: conn)
    monkeypatch.setattr(app, "list_videos_needing_sync", lambda c: [])

    result = app.handler({}, None)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body == {"pushed": 0, "failed": 0, "results": []}
    assert conn.closed is True


def test_handler_increments_sort_order_per_product_only_on_success(monkeypatch):
    conn = _FakeConnection()
    videos = [
        {"video_id": "vid-1", "product_id": "prod-1", "bigcommerce_product_id": "4390",
         "youtube_video_id": "yt-1", "title": "T1", "summary": "S1", "already_synced_count": 1},
        {"video_id": "vid-2", "product_id": "prod-1", "bigcommerce_product_id": "4390",
         "youtube_video_id": "yt-2", "title": "T2", "summary": "S2", "already_synced_count": 1},
        {"video_id": "vid-3", "product_id": "prod-2", "bigcommerce_product_id": "9001",
         "youtube_video_id": "yt-3", "title": "T3", "summary": "S3", "already_synced_count": 0},
    ]
    monkeypatch.setattr(app, "get_db_connection", lambda: conn)
    monkeypatch.setattr(app, "list_videos_needing_sync", lambda c: videos)
    monkeypatch.setattr(app, "get_bigcommerce_credentials", lambda: ("store-hash-1", "token-123"))

    seen_sort_orders = []

    def _fake_process(session, conn_, store_hash, auth_token, video, sort_order):
        seen_sort_orders.append((video["video_id"], sort_order))
        # vid-1 (the first video for prod-1) fails; everything else succeeds.
        if video["video_id"] == "vid-1":
            return {"video_id": video["video_id"], "success": False, "error": "boom"}
        return {"video_id": video["video_id"], "success": True}

    monkeypatch.setattr(app, "_process_one_video", _fake_process)

    result = app.handler({}, None)

    # prod-1: vid-1 seeded at already_synced_count=1 -> sort_order 1, fails, so the
    # counter is NOT advanced -- a failed push must not consume a sort_order slot.
    # vid-2 (also prod-1) therefore sees sort_order 1 too, not 2, and succeeds.
    # prod-2: vid-3 seeded at already_synced_count=0 -> sort_order 0, independent counter.
    assert seen_sort_orders == [("vid-1", 1), ("vid-2", 1), ("vid-3", 0)]

    body = json.loads(result["body"])
    assert body["pushed"] == 2
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
