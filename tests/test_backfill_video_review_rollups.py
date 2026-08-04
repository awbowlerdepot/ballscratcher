"""
Tests for scripts/backfill_video_review_rollups.py.

Manual-runner pattern, run standalone via
`python3 tests/test_backfill_video_review_rollups.py`. Same sys.modules-
faking approach for `requests` as test_auto_approve_video_candidates.py
(this script does `import requests` inside its functions, not at module
top level) -- mirrors that file's structure closely since both scripts
share the same "list from admin API, act on each item, tolerate per-item
errors" shape.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import backfill_video_review_rollups as script  # noqa: E402


# --- list_products_needing_refresh: fake requests, paginates ---

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeRequestsModule:
    def __init__(self, pages=None):
        self.pages = pages if pages is not None else []
        self.get_calls = []
        self.post_calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.get_calls.append({"url": url, "params": params, "headers": headers})
        page = self.pages.pop(0)
        return _FakeResponse(page)

    def post(self, url, headers=None, timeout=None):
        self.post_calls.append({"url": url, "headers": headers})
        return _FakeResponse({"product_id": url.rsplit("/", 2)[1], "rollup_regenerated": True, "video_count": 2})


def test_list_products_needing_refresh_paginates():
    real_requests = sys.modules.get("requests")
    fake = _FakeRequestsModule(pages=[
        {"items": [{"id": f"prod-{i}", "name": f"Ball {i}"} for i in range(200)]},
        {"items": [{"id": "prod-200", "name": "Ball 200"}]},
    ])
    sys.modules["requests"] = fake
    try:
        result = script.list_products_needing_refresh("https://admin.example", "tok", page_limit=200)
        assert len(result) == 201
        assert len(fake.get_calls) == 2
        assert fake.get_calls[0]["params"] == {"needs_video_summary_refresh": "true", "limit": 200, "offset": 0}
        assert fake.get_calls[1]["params"] == {"needs_video_summary_refresh": "true", "limit": 200, "offset": 200}
        assert fake.get_calls[0]["headers"]["Authorization"] == "Bearer tok"
    finally:
        if real_requests is not None:
            sys.modules["requests"] = real_requests
        else:
            del sys.modules["requests"]


def test_list_products_needing_refresh_single_page_stops_immediately():
    real_requests = sys.modules.get("requests")
    fake = _FakeRequestsModule(pages=[{"items": [{"id": "prod-1", "name": "Absolute"}]}])
    sys.modules["requests"] = fake
    try:
        result = script.list_products_needing_refresh("https://admin.example", "tok", page_limit=200)
        assert len(result) == 1
        assert len(fake.get_calls) == 1  # short page (< page_limit) -- no second request
    finally:
        if real_requests is not None:
            sys.modules["requests"] = real_requests
        else:
            del sys.modules["requests"]


def test_list_products_needing_refresh_all_uses_has_approved_video_summaries_param():
    """refresh_all=True should swap the filter param entirely (not add a
    second one) -- see the module docstring's REFRESH_ALL section and
    list_products_needing_refresh's own docstring."""
    real_requests = sys.modules.get("requests")
    fake = _FakeRequestsModule(pages=[{"items": [{"id": "prod-1", "name": "Absolute"}]}])
    sys.modules["requests"] = fake
    try:
        result = script.list_products_needing_refresh("https://admin.example", "tok", page_limit=200, refresh_all=True)
        assert len(result) == 1
        assert fake.get_calls[0]["params"] == {"has_approved_video_summaries": "true", "limit": 200, "offset": 0}
    finally:
        if real_requests is not None:
            sys.modules["requests"] = real_requests
        else:
            del sys.modules["requests"]


# --- refresh_product: fake requests, posts to the right URL ---

def test_refresh_product_posts_to_refresh_video_summary_and_returns_json():
    real_requests = sys.modules.get("requests")
    fake = _FakeRequestsModule()
    sys.modules["requests"] = fake
    try:
        result = script.refresh_product("https://admin.example", "tok", "prod-1")
        assert len(fake.post_calls) == 1
        call = fake.post_calls[0]
        assert call["url"] == "https://admin.example/products/prod-1/refresh-video-summary"
        assert call["headers"]["Authorization"] == "Bearer tok"
        assert result == {"product_id": "prod-1", "rollup_regenerated": True, "video_count": 2}
    finally:
        if real_requests is not None:
            sys.modules["requests"] = real_requests
        else:
            del sys.modules["requests"]


# --- run: the orchestration -- refreshes every listed product, tolerates
# per-product errors, distinguishes "regenerated" from "skipped (no
# summaries)" in the summary counts (only "regenerated" increments
# refreshed) ---

def test_run_refreshes_every_listed_product(monkeypatch):
    products = [
        {"id": "prod-1", "name": "Absolute"},
        {"id": "prod-2", "name": "Nightroad"},
    ]
    monkeypatch.setattr(script, "list_products_needing_refresh", lambda url, token, refresh_all=False: products)

    refreshed_calls = []

    def fake_refresh(url, token, product_id):
        refreshed_calls.append(product_id)
        return {"product_id": product_id, "rollup_regenerated": True, "video_count": 3}

    monkeypatch.setattr(script, "refresh_product", fake_refresh)

    summary = script.run("https://admin.example", "tok")

    assert summary == {"total": 2, "refreshed": 2, "errors": 0}
    assert refreshed_calls == ["prod-1", "prod-2"]


def test_run_does_not_count_no_summaries_result_as_refreshed(monkeypatch):
    """A real, expected outcome (see refresh_video_reviews_rollup's
    docstring): a product can be listed as needing a refresh, then have its
    videos reassigned/deleted out from under it before this script gets to
    it, leaving it with zero approved+summarized videos by the time the
    refresh call actually runs. rollup_regenerated: False in that case --
    not an error, but shouldn't inflate the 'refreshed' count either."""
    products = [{"id": "prod-1", "name": "Absolute"}]
    monkeypatch.setattr(script, "list_products_needing_refresh", lambda url, token, refresh_all=False: products)
    monkeypatch.setattr(script, "refresh_product", lambda url, token, pid:
                         {"product_id": pid, "rollup_regenerated": False, "reason": "no_summaries"})

    summary = script.run("https://admin.example", "tok")

    assert summary == {"total": 1, "refreshed": 0, "errors": 0}


def test_run_tolerates_per_product_refresh_errors(monkeypatch):
    """One product failing (e.g. a transient Bedrock hiccup) shouldn't stop
    the rest of the batch -- same principle as auto_approve_video_
    candidates.run() and home_transcript_fetcher.run()."""
    products = [{"id": "prod-1", "name": "Absolute"}, {"id": "prod-2", "name": "Nightroad"}]
    monkeypatch.setattr(script, "list_products_needing_refresh", lambda url, token, refresh_all=False: products)

    def flaky_refresh(url, token, product_id):
        if product_id == "prod-1":
            raise RuntimeError("simulated Bedrock failure")
        return {"product_id": product_id, "rollup_regenerated": True, "video_count": 1}

    monkeypatch.setattr(script, "refresh_product", flaky_refresh)

    summary = script.run("https://admin.example", "tok")

    assert summary == {"total": 2, "refreshed": 1, "errors": 1}


def test_run_with_no_products_needing_refresh():
    summary = script.run("https://admin.example", "tok", list_fn=lambda url, token: [])
    assert summary == {"total": 0, "refreshed": 0, "errors": 0}


def test_run_refresh_all_threads_through_to_default_list_fn(monkeypatch):
    """run(refresh_all=True) with no explicit list_fn should build its
    default closure so that list_products_needing_refresh actually receives
    refresh_all=True -- this is what makes REFRESH_ALL=true do the broader
    has_approved_video_summaries pass instead of the default staleness
    check. A caller-supplied list_fn (like test_run_with_no_products_
    needing_refresh above) intentionally bypasses this -- refresh_all only
    affects the default."""
    calls = []

    def fake_list(url, token, refresh_all=False):
        calls.append(refresh_all)
        return []

    monkeypatch.setattr(script, "list_products_needing_refresh", fake_list)

    summary = script.run("https://admin.example", "tok", refresh_all=True)

    assert calls == [True]
    assert summary == {"total": 0, "refreshed": 0, "errors": 0}


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
