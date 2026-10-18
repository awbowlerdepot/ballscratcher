"""
Tests for src/woocommerce_url_discovery/app.py, run against real captured
category-page and sitemap data (see the header comments in
tests/fixtures/swag_bowling_balls_page1.html, page2.html, and
swag_product_sitemap_sample.xml for exactly what's real). Written to run
standalone via `python3 tests/test_woocommerce_url_discovery.py`, same
manual-runner pattern as the pdf_parser/image_processor/admin_api tests --
unlike test_url_discovery.py (the Craft-CMS version), this one doesn't
need pytest to actually execute in this sandbox.
"""
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "woocommerce_url_discovery"))

import app  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

PAGE1_URL = "https://www.swagbowling.com/shop/bowling-balls/"
PAGE2_URL = "https://www.swagbowling.com/shop/bowling-balls/page/2/"


def _load(name):
    return (FIXTURES / name).read_text()


def test_parse_category_page_extracts_product_links():
    html = _load("swag_bowling_balls_page1.html")
    urls = app.parse_category_page(html, PAGE1_URL)
    assert urls == {
        "https://www.swagbowling.com/product/swag-insanity-pearl-bowling-ball/",
        "https://www.swagbowling.com/product/swag-fusion-bowling-ball/",
        "https://www.swagbowling.com/product/swag-judge-pearl-bowling-ball/",
        "https://www.swagbowling.com/product/swag-ace-bowling-ball/",
        "https://www.swagbowling.com/product/swag-big-bro-assemble-bowling-ball/",
    }


def test_find_next_page_url_finds_page_2():
    html = _load("swag_bowling_balls_page1.html")
    next_url = app.find_next_page_url(html, PAGE1_URL, current_page=1)
    assert next_url == PAGE2_URL


def test_find_next_page_url_none_on_last_page():
    html = _load("swag_bowling_balls_page2.html")
    next_url = app.find_next_page_url(html, PAGE2_URL, current_page=2)
    assert next_url is None


def test_discover_ball_urls_paginates_across_both_real_pages():
    """Confirms the pagination loop actually follows page 1 -> page 2 and
    unions the results, using the two real captured pages."""
    fixtures_by_url = {
        PAGE1_URL: _load("swag_bowling_balls_page1.html"),
        PAGE2_URL: _load("swag_bowling_balls_page2.html"),
    }

    def fake_fetch(url):
        return fixtures_by_url[url]

    urls = app.discover_ball_urls(fake_fetch, start_url=PAGE1_URL)
    assert len(urls) == 6  # 5 from page 1 + 1 from page 2
    assert "https://www.swagbowling.com/product/swag-goat-bowling-ball/" in urls


def test_parse_sitemap_lastmods_returns_real_values():
    xml_bytes = (FIXTURES / "swag_product_sitemap_sample.xml").read_bytes()
    lastmods = app.parse_sitemap_lastmods(xml_bytes)
    assert len(lastmods) == 3
    assert lastmods["https://www.swagbowling.com/product/swag-swagger-bowling-ball/"] == "2025-01-13T15:15:43+00:00"


def test_build_entries_missing_sitemap_entry_gets_none_lastmod():
    """A ball URL discovered via the category page but not yet present in
    the sitemap sample (e.g. Fusion, not included in this session's
    trimmed sitemap fixture) should still be included, with lastmod=None
    rather than being dropped."""
    ball_urls = {
        "https://www.swagbowling.com/product/swag-swagger-bowling-ball/",
        "https://www.swagbowling.com/product/swag-fusion-bowling-ball/",
    }
    lastmods = {"https://www.swagbowling.com/product/swag-swagger-bowling-ball/": "2025-01-13T15:15:43+00:00"}

    entries = app.build_entries(ball_urls, lastmods)
    by_url = {e["url"]: e for e in entries}

    assert by_url["https://www.swagbowling.com/product/swag-swagger-bowling-ball/"]["lastmod"] == "2025-01-13T15:15:43+00:00"
    assert by_url["https://www.swagbowling.com/product/swag-fusion-bowling-ball/"]["lastmod"] is None


# --- diff_against_known: same fake-cursor pattern as url_discovery's tests ---

class FakeCursor:
    def __init__(self, known):
        self._known = known
        self._result = None

    def execute(self, query, params=None):
        q = query.strip().lower()
        if q.startswith("select"):
            (url,) = params
            v = self._known.get(url)
            self._result = (v,) if v is not None else None
        elif q.startswith("insert"):
            _brand_id, url, lastmod = params
            self._known[url] = lastmod
        elif q.startswith("update") and "sitemap_lastmod" in q:
            lastmod, url = params
            self._known[url] = lastmod

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


def test_diff_against_known_new_url():
    conn = FakeConnection(known={})
    entries = [{"url": "https://www.swagbowling.com/product/swag-fusion-bowling-ball/", "lastmod": "2025-01-13T15:15:43+00:00"}]
    diff = app.diff_against_known(conn, brand_id="fake-brand", entries=entries)
    assert diff["new"] == ["https://www.swagbowling.com/product/swag-fusion-bowling-ball/"]
    assert conn.committed


def test_diff_against_known_unchanged():
    url = "https://www.swagbowling.com/product/swag-fusion-bowling-ball/"
    conn = FakeConnection(known={url: "2025-01-13T15:15:43+00:00"})
    entries = [{"url": url, "lastmod": "2025-01-13T15:15:43+00:00"}]
    diff = app.diff_against_known(conn, brand_id="fake-brand", entries=entries)
    assert diff["unchanged"] == [url]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS: {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")


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
