"""
Tests for scripts/rescrape_commercebuild_products.py.

Manual-runner pattern, run standalone via
`python3 tests/test_rescrape_commercebuild_products.py`.

Same injectable-session pattern as test_rescrape_netsuite_products.py's
tests, for the same reason (get_requests_session touches requests.
Session/requests.adapters.HTTPAdapter/urllib3.util.retry.Retry, so a bare
fake module swap wouldn't cover it) -- this script's list/rescrape/run/
retry shape is a direct copy of rescrape_netsuite_products.py's, just
scoped by BOTH source_platform=commercebuild AND missing_skus=true
instead of source_platform=netsuite alone, so these tests mirror that
file's almost exactly.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import rescrape_commercebuild_products as script  # noqa: E402


# --- list_commercebuild_products_missing_skus / rescrape_product: fake
# injected session ---

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
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
        return _FakeResponse({"queued": True, "product_id": url.rsplit("/", 2)[1],
                               "url": "https://www.stormbowling.com/x"})


def test_list_commercebuild_products_missing_skus_paginates():
    fake = _FakeSession(pages=[
        {"items": [{"id": f"prod-{i}", "name": f"Ball {i}"} for i in range(200)]},
        {"items": [{"id": "prod-200", "name": "Ball 200"}]},
    ])

    result = script.list_commercebuild_products_missing_skus("https://admin.example", "tok", page_limit=200, session=fake)

    assert len(result) == 201
    assert len(fake.get_calls) == 2
    assert fake.get_calls[0]["params"] == {
        "source_platform": "commercebuild", "missing_skus": "true", "limit": 200, "offset": 0,
    }
    assert fake.get_calls[1]["params"] == {
        "source_platform": "commercebuild", "missing_skus": "true", "limit": 200, "offset": 200,
    }
    assert fake.get_calls[0]["headers"]["Authorization"] == "Bearer tok"


def test_list_commercebuild_products_missing_skus_single_page_stops_immediately():
    fake = _FakeSession(pages=[{"items": [{"id": "prod-1", "name": "Tropical Surge Black/Cherry"}]}])

    result = script.list_commercebuild_products_missing_skus("https://admin.example", "tok", page_limit=200, session=fake)

    assert len(result) == 1
    assert len(fake.get_calls) == 1  # short page (< page_limit) -- no second request


def test_rescrape_product_posts_to_rescrape_and_returns_json():
    fake = _FakeSession()

    result = script.rescrape_product("https://admin.example", "tok", "prod-1", session=fake)

    assert len(fake.post_calls) == 1
    call = fake.post_calls[0]
    assert call["url"] == "https://admin.example/products/prod-1/rescrape"
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert result == {"queued": True, "product_id": "prod-1", "url": "https://www.stormbowling.com/x"}


# --- get_requests_session: same retry-config sanity check as every other
# script in this project.

def test_get_requests_session_retries_on_throttle_and_5xx_status_codes():
    session = script.get_requests_session()
    adapter = session.get_adapter("https://admin.example")
    retry = adapter.max_retries

    assert retry.total == script.RETRY_TOTAL
    assert set(retry.status_forcelist) == set(script.RETRY_STATUS_FORCELIST)
    assert 503 in retry.status_forcelist
    assert "GET" in retry.allowed_methods
    assert "POST" in retry.allowed_methods
    assert retry.backoff_factor == script.RETRY_BACKOFF_FACTOR


def test_get_requests_session_returns_a_fresh_session_each_call():
    assert script.get_requests_session() is not script.get_requests_session()


# --- run: the orchestration -- rescrapes every listed product, tolerates
# per-product errors, distinguishes "queued" from "skipped" from "errored" ---

def test_run_queues_every_listed_product(monkeypatch):
    products = [
        {"id": "prod-1", "name": "Tropical Surge Black/Cherry"},
        {"id": "prod-2", "name": "Alpha Crux"},
    ]
    monkeypatch.setattr(script, "list_commercebuild_products_missing_skus", lambda url, token: products)

    rescraped_calls = []

    def fake_rescrape(url, token, product_id):
        rescraped_calls.append(product_id)
        return {"queued": True, "product_id": product_id, "url": "https://www.stormbowling.com/x"}

    monkeypatch.setattr(script, "rescrape_product", fake_rescrape)

    summary = script.run("https://admin.example", "tok")

    assert summary == {"total": 2, "queued": 2, "skipped": 0, "errors": 0}
    assert rescraped_calls == ["prod-1", "prod-2"]


def test_run_counts_unqueueable_as_skipped_not_errored(monkeypatch):
    """Shouldn't really happen for a commercebuild-sourced product (see
    run's own docstring -- source_platform='commercebuild' always
    resolves to a real queue env var key), but queue_rescrape can still
    return queued=False if that queue's env var isn't actually configured
    on this deployment -- an expected, non-error outcome either way, same
    convention as backfill_core_ids.py/rescrape_netsuite_products.py."""
    products = [{"id": "prod-1", "name": "Some Storm Ball"}]
    monkeypatch.setattr(script, "list_commercebuild_products_missing_skus", lambda url, token: products)
    monkeypatch.setattr(script, "rescrape_product", lambda url, token, pid:
                         {"queued": False, "reason": "COMMERCEBUILD_PRODUCT_SCRAPE_QUEUE_URL is not configured on this deployment"})

    summary = script.run("https://admin.example", "tok")

    assert summary == {"total": 1, "queued": 0, "skipped": 1, "errors": 0}


def test_run_tolerates_per_product_rescrape_errors(monkeypatch):
    """One product failing (e.g. a transient SQS hiccup) shouldn't stop the
    rest of the batch -- same principle as every other batch script in
    this project."""
    products = [{"id": "prod-1", "name": "Tropical Surge Black/Cherry"}, {"id": "prod-2", "name": "Alpha Crux"}]
    monkeypatch.setattr(script, "list_commercebuild_products_missing_skus", lambda url, token: products)

    def flaky_rescrape(url, token, product_id):
        if product_id == "prod-1":
            raise RuntimeError("simulated SQS failure")
        return {"queued": True, "product_id": product_id, "url": "https://www.stormbowling.com/x"}

    monkeypatch.setattr(script, "rescrape_product", flaky_rescrape)

    summary = script.run("https://admin.example", "tok")

    assert summary == {"total": 2, "queued": 1, "skipped": 0, "errors": 1}


def test_run_with_no_matching_products():
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
