"""
Tests for scripts/backfill_core_ids.py.

Manual-runner pattern, run standalone via
`python3 tests/test_backfill_core_ids.py`. Same sys.modules-faking approach
for `requests` as test_backfill_video_review_rollups.py -- mirrors that
file's structure closely since both scripts share the same "list from
admin API, act on each item, tolerate per-item errors" shape (list ->
POST-per-item -> tally queued/skipped/errors).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import backfill_core_ids as script  # noqa: E402


# --- list_products_missing_core: fake requests, paginates ---

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
        return _FakeResponse({"queued": True, "product_id": url.rsplit("/", 2)[1], "url": "https://example.com/x"})


def test_list_products_missing_core_paginates():
    real_requests = sys.modules.get("requests")
    fake = _FakeRequestsModule(pages=[
        {"items": [{"id": f"prod-{i}", "name": f"Ball {i}"} for i in range(200)]},
        {"items": [{"id": "prod-200", "name": "Ball 200"}]},
    ])
    sys.modules["requests"] = fake
    try:
        result = script.list_products_missing_core("https://admin.example", "tok", page_limit=200)
        assert len(result) == 201
        assert len(fake.get_calls) == 2
        assert fake.get_calls[0]["params"] == {"missing_core": "true", "limit": 200, "offset": 0}
        assert fake.get_calls[1]["params"] == {"missing_core": "true", "limit": 200, "offset": 200}
        assert fake.get_calls[0]["headers"]["Authorization"] == "Bearer tok"
    finally:
        if real_requests is not None:
            sys.modules["requests"] = real_requests
        else:
            del sys.modules["requests"]


def test_list_products_missing_core_single_page_stops_immediately():
    real_requests = sys.modules.get("requests")
    fake = _FakeRequestsModule(pages=[{"items": [{"id": "prod-1", "name": "Absolute"}]}])
    sys.modules["requests"] = fake
    try:
        result = script.list_products_missing_core("https://admin.example", "tok", page_limit=200)
        assert len(result) == 1
        assert len(fake.get_calls) == 1  # short page (< page_limit) -- no second request
    finally:
        if real_requests is not None:
            sys.modules["requests"] = real_requests
        else:
            del sys.modules["requests"]


# --- rescrape_product: fake requests, posts to the right URL ---

def test_rescrape_product_posts_to_rescrape_and_returns_json():
    real_requests = sys.modules.get("requests")
    fake = _FakeRequestsModule()
    sys.modules["requests"] = fake
    try:
        result = script.rescrape_product("https://admin.example", "tok", "prod-1")
        assert len(fake.post_calls) == 1
        call = fake.post_calls[0]
        assert call["url"] == "https://admin.example/products/prod-1/rescrape"
        assert call["headers"]["Authorization"] == "Bearer tok"
        assert result == {"queued": True, "product_id": "prod-1", "url": "https://example.com/x"}
    finally:
        if real_requests is not None:
            sys.modules["requests"] = real_requests
        else:
            del sys.modules["requests"]


# --- run: the orchestration -- rescrapes every listed product, tolerates
# per-product errors, distinguishes "queued" from "skipped (unsupported
# platform)" from "errored" in the summary counts ---

def test_run_queues_every_listed_product(monkeypatch):
    products = [
        {"id": "prod-1", "name": "Absolute"},
        {"id": "prod-2", "name": "Nightroad"},
    ]
    monkeypatch.setattr(script, "list_products_missing_core", lambda url, token: products)

    rescraped_calls = []

    def fake_rescrape(url, token, product_id):
        rescraped_calls.append(product_id)
        return {"queued": True, "product_id": product_id, "url": "https://example.com/x"}

    monkeypatch.setattr(script, "rescrape_product", fake_rescrape)

    summary = script.run("https://admin.example", "tok")

    assert summary == {"total": 2, "queued": 2, "skipped": 0, "errors": 0}
    assert rescraped_calls == ["prod-1", "prod-2"]


def test_run_counts_unsupported_platform_as_skipped_not_errored(monkeypatch):
    """A real, expected outcome (see queue_rescrape's docstring): a product
    on a platform with no scraper deployed yet (shopify) returns
    queued=False with a reason, not an exception -- shouldn't count as an
    error, and shouldn't inflate the 'queued' count either."""
    products = [{"id": "prod-1", "name": "Some Hammer Ball"}]
    monkeypatch.setattr(script, "list_products_missing_core", lambda url, token: products)
    monkeypatch.setattr(script, "rescrape_product", lambda url, token, pid:
                         {"queued": False, "reason": "no scraper deployed for source_platform='shopify' yet"})

    summary = script.run("https://admin.example", "tok")

    assert summary == {"total": 1, "queued": 0, "skipped": 1, "errors": 0}


def test_run_tolerates_per_product_rescrape_errors(monkeypatch):
    """One product failing (e.g. a transient SQS hiccup) shouldn't stop the
    rest of the batch -- same principle as backfill_video_review_rollups.
    run(), auto_approve_video_candidates.run(), home_transcript_fetcher.
    run()."""
    products = [{"id": "prod-1", "name": "Absolute"}, {"id": "prod-2", "name": "Nightroad"}]
    monkeypatch.setattr(script, "list_products_missing_core", lambda url, token: products)

    def flaky_rescrape(url, token, product_id):
        if product_id == "prod-1":
            raise RuntimeError("simulated SQS failure")
        return {"queued": True, "product_id": product_id, "url": "https://example.com/x"}

    monkeypatch.setattr(script, "rescrape_product", flaky_rescrape)

    summary = script.run("https://admin.example", "tok")

    assert summary == {"total": 2, "queued": 1, "skipped": 0, "errors": 1}


def test_run_with_no_products_missing_core():
    summary = script.run("https://admin.example", "tok", list_fn=lambda url, token: [])
    assert summary == {"total": 0, "queued": 0, "skipped": 0, "errors": 0}


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
