"""
Tests for src/shopify_url_discovery/app.py, run against real captured
collection-listing JSON (see tests/fixtures/hammer_collection_high_
performance.json and hammer_collection_retired_balls.json's real handle/
updated_at values, trimmed from live hammerbowling.com/collections/*/
products.json fetches this session). Manual-runner pattern, run standalone
via `python3 tests/test_shopify_url_discovery.py`, same as every other
scraper's test file in this project.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "shopify_url_discovery"))

import app  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
STORE_DOMAIN = "hammerbowling.com"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


# --- discover_collection_products: pagination against a fake fetch_fn ---

def test_discover_collection_products_stops_on_short_page():
    calls = []

    def fake_fetch(store_domain, handle, page, limit):
        calls.append(page)
        return _load("hammer_collection_high_performance.json")  # 5 products, well under any real limit

    products = app.discover_collection_products(fake_fetch, STORE_DOMAIN, "high-performance", limit=250)
    assert len(products) == 5
    assert calls == [1]  # short page (5 < 250) -- no second request


def test_discover_collection_products_paginates_on_full_page():
    pages = [
        {"products": [{"id": i, "handle": f"ball-{i}", "updated_at": "2026-01-01T00:00:00-04:00"} for i in range(3)]},
        {"products": [{"id": 99, "handle": "ball-99", "updated_at": "2026-01-01T00:00:00-04:00"}]},
    ]
    calls = []

    def fake_fetch(store_domain, handle, page, limit):
        calls.append(page)
        return pages[page - 1]

    products = app.discover_collection_products(fake_fetch, STORE_DOMAIN, "high-performance", limit=3)
    assert len(products) == 4
    assert calls == [1, 2]


# --- build_entries: current-wins classification + URL construction ---

def test_build_entries_builds_product_urls_from_handles():
    products_by_handle = {"high-performance": _load("hammer_collection_high_performance.json")["products"]}
    entries = app.build_entries(STORE_DOMAIN, products_by_handle)
    by_url = {e["url"]: e for e in entries}
    assert "https://hammerbowling.com/products/spawn" in by_url
    assert by_url["https://hammerbowling.com/products/spawn"]["status"] == "current"
    assert by_url["https://hammerbowling.com/products/spawn"]["lastmod"] == "2026-08-05T01:37:05-04:00"


def test_build_entries_retired_collection_marks_status_retired():
    products_by_handle = {"retired-balls": _load("hammer_collection_retired_balls.json")["products"]}
    entries = app.build_entries(STORE_DOMAIN, products_by_handle)
    by_url = {e["url"]: e for e in entries}
    assert by_url["https://hammerbowling.com/products/3-d-offset"]["status"] == "retired"


def test_build_entries_current_wins_when_product_in_both_collections():
    """Real, confirmed-possible edge case per the module docstring: a
    product indexed under both a current-tier collection and
    retired-balls should end up 'current', not 'retired'."""
    products_by_handle = {
        "retired-balls": [{"id": 1, "handle": "some-ball", "updated_at": "2026-01-01T00:00:00-04:00"}],
        "high-performance": [{"id": 1, "handle": "some-ball", "updated_at": "2026-01-02T00:00:00-04:00"}],
    }
    entries = app.build_entries(STORE_DOMAIN, products_by_handle)
    assert len(entries) == 1
    assert entries[0]["status"] == "current"


def test_build_entries_collapses_duplicate_handle_to_one_entry():
    products_by_handle = {
        "high-performance": [{"id": 1, "handle": "spawn", "updated_at": "2026-01-01T00:00:00-04:00"}],
        "upper-mid-performance": [{"id": 1, "handle": "spawn", "updated_at": "2026-01-01T00:00:00-04:00"}],
    }
    entries = app.build_entries(STORE_DOMAIN, products_by_handle)
    assert len(entries) == 1


# --- diff_against_known: same fake-cursor pattern as every other family's tests ---

class FakeCursor:
    def __init__(self, known):
        self._known = known  # url -> (status, lastmod)
        self._result = None

    def execute(self, query, params=None):
        q = query.strip().lower()
        if q.startswith("select"):
            (url,) = params
            row = self._known.get(url)
            self._result = (row[1],) if row is not None else None
        elif q.startswith("insert"):
            _brand_id, url, status, lastmod = params
            self._known[url] = (status, lastmod)
        elif q.startswith("update") and "sitemap_lastmod" in q:
            lastmod, status, url = params
            self._known[url] = (status, lastmod)
        elif q.startswith("update"):  # unchanged-path status_path-only update
            status, url = params
            existing_status, existing_lastmod = self._known.get(url, (None, None))
            self._known[url] = (status, existing_lastmod)

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, known):
        self._cursor = FakeCursor(known)
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


def test_diff_against_known_new_url_records_status():
    conn = FakeConnection(known={})
    entries = [{"url": "https://hammerbowling.com/products/spawn", "lastmod": "2026-08-05T01:37:05-04:00", "status": "current"}]
    diff = app.diff_against_known(conn, brand_id="fake-brand", entries=entries)
    assert diff["new"] == ["https://hammerbowling.com/products/spawn"]
    assert conn._cursor._known["https://hammerbowling.com/products/spawn"] == ("current", "2026-08-05T01:37:05-04:00")
    assert conn.committed


def test_diff_against_known_changed_lastmod():
    url = "https://hammerbowling.com/products/spawn"
    conn = FakeConnection(known={url: ("current", "2026-08-05T01:37:05-04:00")})
    entries = [{"url": url, "lastmod": "2026-08-06T00:00:00-04:00", "status": "current"}]
    diff = app.diff_against_known(conn, brand_id="fake-brand", entries=entries)
    assert diff["changed"] == [url]


def test_diff_against_known_unchanged():
    url = "https://hammerbowling.com/products/spawn"
    conn = FakeConnection(known={url: ("current", "2026-08-05T01:37:05-04:00")})
    entries = [{"url": url, "lastmod": "2026-08-05T01:37:05-04:00", "status": "current"}]
    diff = app.diff_against_known(conn, brand_id="fake-brand", entries=entries)
    assert diff["unchanged"] == [url]


# --- build_scrape_messages / publish_messages: same shape as every other family ---

def test_build_scrape_messages():
    messages = app.build_scrape_messages("brand-1", ["https://hammerbowling.com/products/spawn"])
    assert json.loads(messages[0]) == {"url": "https://hammerbowling.com/products/spawn", "brand_id": "brand-1"}


def test_publish_messages_batches_over_10():
    class FakeSqs:
        def __init__(self):
            self.batches = []

        def send_message_batch(self, QueueUrl, Entries):
            self.batches.append(Entries)

    sqs = FakeSqs()
    sent = app.publish_messages(sqs, "q-url", [f"m{i}" for i in range(12)])
    assert sent == 12
    assert [len(b) for b in sqs.batches] == [10, 2]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS: {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")
