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
    def __init__(self, known, fingerprints=None):
        self._known = known  # url -> (status, lastmod)
        self.fingerprints = fingerprints if fingerprints is not None else {}  # url -> content_fingerprint
        self._result = None

    def execute(self, query, params=None):
        q = " ".join(query.split()).lower()
        if q.startswith("select"):
            (url,) = params
            row = self._known.get(url)
            self._result = (row[1], self.fingerprints.get(url)) if row is not None else None
        elif q.startswith("insert"):
            _brand_id, url, status, lastmod, fp = params
            self._known[url] = (status, lastmod)
            self.fingerprints[url] = fp
        elif "scrape_status = 'pending'" in q:  # changed path
            lastmod, fp, status, url = params
            self._known[url] = (status, lastmod)
            self.fingerprints[url] = fp
        elif q.startswith("update"):  # unchanged path: coalesce lastmod/fingerprint
            status, lastmod, fp, url = params
            _, old_lastmod = self._known.get(url, (None, None))
            self._known[url] = (status, lastmod if lastmod is not None else old_lastmod)
            if fp is not None:
                self.fingerprints[url] = fp

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


def test_build_entries_columbia_retired_balls_1_is_retired():
    # Runbook 6dn: Columbia 300's retired collection is "retired-balls-1".
    products_by_handle = {"retired-balls-1": [{"handle": "piranha-solid", "updated_at": "2026-02-19T00:00:00-05:00"}]}
    entries = app.build_entries("columbia300.com", products_by_handle)
    assert entries[0]["status"] == "retired"


# --- lastmod_changed / full_refresh (runbook 6dx) ---

import datetime as _dt  # noqa: E402
import importlib as _importlib  # noqa: E402

_disc = _importlib.import_module("app")


def test_lastmod_changed_compares_instants_not_strings():
    # The real bug: psycopg2 returns a tz-aware datetime in UTC, the feed
    # gives the same moment in the site's own offset. Must be "unchanged".
    stored = _dt.datetime(2025, 7, 15, 13, 14, 20, tzinfo=_dt.timezone.utc)
    assert _disc.lastmod_changed(stored, "2025-07-15T09:14:20-04:00") is False
    assert _disc.lastmod_changed(stored, "2025-07-15T13:14:20Z") is False
    # A real edit is still caught.
    assert _disc.lastmod_changed(stored, "2025-07-16T09:14:20-04:00") is True
    # Same rules when the stored value comes back as a string (test fakes).
    assert _disc.lastmod_changed("2025-07-15 13:14:20+00:00", "2025-07-15T09:14:20-04:00") is False


def test_lastmod_changed_edge_cases_match_the_old_behavior():
    assert _disc.lastmod_changed("2025-07-15T09:14:20-04:00", None) is False   # no incoming lastmod
    assert _disc.lastmod_changed(None, "2025-07-15T09:14:20-04:00") is True    # first lastmod seen
    assert _disc.lastmod_changed("garbage", "also garbage") is True            # unparseable: string compare
    assert _disc.lastmod_changed("same", "same") is False


def test_handler_full_refresh_queues_unchanged_urls_too():
    import types

    sent = []
    diff = {"new": ["n"], "changed": ["c"], "unchanged": ["u1", "u2"]}
    saved = {k: getattr(_disc, k) for k in ("diff_against_known", "get_db_connection", "publish_messages")}
    fakes = {}
    for name in dir(_disc):
        if name.startswith(("fetch_", "parse_", "discover_", "collect_", "list_")) and callable(getattr(_disc, name)):
            fakes[name] = getattr(_disc, name)
    try:
        for name in fakes:
            setattr(_disc, name, lambda *a, **k: [])
        _disc.get_db_connection = lambda: types.SimpleNamespace(close=lambda: None)
        _disc.diff_against_known = lambda *a, **k: diff
        _disc.publish_messages = lambda sqs, q, msgs: sent.append(list(msgs)) or len(msgs)
        os.environ.setdefault("BRAND_ID", "b")
        os.environ["PRODUCT_SCRAPE_QUEUE_URL"] = "q"
        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.client = lambda *a, **k: object()
        real_boto3 = sys.modules.get("boto3")
        sys.modules["boto3"] = fake_boto3
        try:
            _disc.handler({}, None)
            _disc.handler({"full_refresh": True}, None)
        finally:
            if real_boto3 is not None:
                sys.modules["boto3"] = real_boto3
            else:
                sys.modules.pop("boto3", None)
    finally:
        for k, v in saved.items():
            setattr(_disc, k, v)
        for k, v in fakes.items():
            setattr(_disc, k, v)
        os.environ.pop("PRODUCT_SCRAPE_QUEUE_URL", None)
    daily, weekly = sent
    assert len(daily) == 2           # new + changed only
    assert len(weekly) == 4          # plus both unchanged URLs


# --- content fingerprint (runbook 6ea) ---

_PRODUCT = {
    "handle": "reckoning", "title": "Reckoning", "body_html": "<h3>BALL SPECS</h3><ul><li>RG 2.49</li></ul>",
    "product_type": "Bowling Ball", "tags": ["high-performance", "new"],
    "updated_at": "2026-10-19T16:21:08-04:00",
    "images": [{"position": 1, "src": "https://cdn.shopify.com/a.png?v=1"}, {"position": 2, "src": "https://cdn.shopify.com/b.png?v=1"}],
    "variants": [{"sku": "H1", "available": True, "price": "189.95", "updated_at": "2026-10-19T16:21:08-04:00"}],
}


def test_fingerprint_ignores_the_store_wide_churn_but_catches_real_page_changes():
    import copy

    base = app.product_fingerprint(_PRODUCT)
    churned = copy.deepcopy(_PRODUCT)
    churned["updated_at"] = "2026-10-19T19:02:44-04:00"           # Shopify re-stamp
    churned["variants"][0].update(available=False, price="179.95")  # stock / price
    churned["tags"] = ["new", "high-performance"]                  # same tags, other order
    assert app.product_fingerprint(churned) == base

    for field, value in (("title", "Reckoning Pearl"), ("body_html", "<h3>BALL SPECS</h3><ul><li>RG 2.50</li></ul>"),
                         ("product_type", "Spare Ball"), ("tags", ["high-performance"])):
        changed = copy.deepcopy(_PRODUCT)
        changed[field] = value
        assert app.product_fingerprint(changed) != base, field
    reimaged = copy.deepcopy(_PRODUCT)
    reimaged["images"][0]["src"] = "https://cdn.shopify.com/a.png?v=2"
    assert app.product_fingerprint(reimaged) != base


def test_build_entries_carries_the_fingerprint():
    entries = app.build_entries("hammerbowling.com", {"high-performance": [_PRODUCT]})
    assert entries[0]["fingerprint"] == app.product_fingerprint(_PRODUCT)


def test_diff_first_fingerprint_is_a_baseline_not_a_change():
    url = "https://hammerbowling.com/products/reckoning"
    conn = FakeConnection(known={url: ("current", "2026-10-18T10:00:00-04:00")})
    entries = [{"url": url, "lastmod": "2026-10-19T16:21:08-04:00", "fingerprint": "fp1", "status": "current"}]
    diff = app.diff_against_known(conn, brand_id="b", entries=entries)
    assert diff["unchanged"] == [url] and diff["changed"] == []
    assert conn._cursor.fingerprints[url] == "fp1"


def test_diff_uses_the_fingerprint_not_updated_at():
    url = "https://hammerbowling.com/products/reckoning"
    conn = FakeConnection(known={url: ("current", "2026-10-18T10:00:00-04:00")})
    conn._cursor.fingerprints[url] = "fp1"
    # updated_at moved, content didn't: unchanged.
    same = [{"url": url, "lastmod": "2026-10-19T16:21:08-04:00", "fingerprint": "fp1", "status": "current"}]
    assert app.diff_against_known(conn, brand_id="b", entries=same)["unchanged"] == [url]
    # content changed: changed, and the new fingerprint is stored.
    moved = [{"url": url, "lastmod": "2026-10-19T16:21:08-04:00", "fingerprint": "fp2", "status": "current"}]
    assert app.diff_against_known(conn, brand_id="b", entries=moved)["changed"] == [url]
    assert conn._cursor.fingerprints[url] == "fp2"
