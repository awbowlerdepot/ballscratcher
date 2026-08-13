"""
Tests for src/price_checker/app.py.

Manual-runner pattern, run standalone via
`python3 tests/test_price_checker.py` -- same convention as
test_video_discovery.py/test_admin_api_service.py (no pytest in this
sandbox, see those files' own header comments).

Honesty note: no real retailer page was fetched this session (no outbound
network access in this sandbox) -- extract_price's tests run against
small hand-written HTML snippets exercising the CSS-selector + price-text
shapes described in this feature's design conversation (a price element
matched by selector, its text parsed for a dollar amount), not a live
capture of any specific site's real markup. Everything DB-facing is
exercised against a fake psycopg2-shaped cursor/connection, same
limitation as every other DB-touching test file in this project (no
Postgres instance available here).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "price_checker"))

import app  # noqa: E402


# --- parse_price: pure, no DB, no network ---

def test_parse_price_simple_dollar_amount():
    assert app.parse_price("$149.99") == 149.99


def test_parse_price_with_thousands_separator():
    assert app.parse_price("$1,499.00") == 1499.00


def test_parse_price_prefers_cents_precision_match_over_bare_integer():
    # "16" (a weight, say, embedded in surrounding text) shouldn't win
    # over the real $149.99 later in the same string.
    assert app.parse_price("16 lb -- $149.99") == 149.99


def test_parse_price_falls_back_to_bare_integer_when_no_cents():
    assert app.parse_price("Now $150") == 150.0


def test_parse_price_takes_first_match_in_a_range():
    assert app.parse_price("$99.99 - $109.99") == 99.99


def test_parse_price_returns_none_for_unparseable_text():
    assert app.parse_price("Call for pricing") is None


def test_parse_price_returns_none_for_blank_text():
    assert app.parse_price("") is None
    assert app.parse_price(None) is None


# --- extract_price: bs4 selector + parse_price, small hand-written HTML ---

def test_extract_price_matches_selector_and_parses_price():
    html = '<html><body><span class="price">$129.99</span></body></html>'
    result = app.extract_price(html, ".price")
    assert result == {"price": 129.99, "raw_price_text": "$129.99", "error": None}


def test_extract_price_no_selector_match_reports_error():
    html = '<html><body><span class="cost">$129.99</span></body></html>'
    result = app.extract_price(html, ".price")
    assert result["price"] is None
    assert result["raw_price_text"] is None
    assert "matched nothing" in result["error"]


def test_extract_price_unparseable_text_reports_error_but_keeps_raw_text():
    html = '<html><body><span class="price">Call for pricing</span></body></html>'
    result = app.extract_price(html, ".price")
    assert result["price"] is None
    assert result["raw_price_text"] == "Call for pricing"
    assert "could not parse a price" in result["error"]


def test_extract_price_collapses_nested_element_whitespace():
    html = '<html><body><span class="price">  $ <b>149</b>.99  <em>USD</em> </span></body></html>'
    result = app.extract_price(html, ".price")
    assert result["price"] == 149.99


# --- check_price_source: never raises, wraps fetch_page/extract_price ---

def test_check_price_source_success(monkeypatch):
    monkeypatch.setattr(app, "fetch_page", lambda url, session=None, timeout=app.DEFAULT_FETCH_TIMEOUT_SECONDS:
                         '<span class="price">$99.99</span>')
    result = app.check_price_source({"product_url": "https://example.com/p", "css_selector": ".price"})
    assert result == {"price": 99.99, "raw_price_text": "$99.99", "error": None}


def test_check_price_source_fetch_failure_does_not_raise(monkeypatch):
    def _boom(url, session=None, timeout=app.DEFAULT_FETCH_TIMEOUT_SECONDS):
        raise ConnectionError("timed out")

    monkeypatch.setattr(app, "fetch_page", _boom)
    result = app.check_price_source({"product_url": "https://example.com/p", "css_selector": ".price"})
    assert result["price"] is None
    assert result["raw_price_text"] is None
    assert "fetch failed" in result["error"]


def test_check_price_source_selector_miss_does_not_raise(monkeypatch):
    monkeypatch.setattr(app, "fetch_page", lambda url, session=None, timeout=app.DEFAULT_FETCH_TIMEOUT_SECONDS:
                         '<span class="cost">$99.99</span>')
    result = app.check_price_source({"product_url": "https://example.com/p", "css_selector": ".price"})
    assert result["price"] is None
    assert "matched nothing" in result["error"]


# --- DB-facing functions: fake psycopg2-shaped cursor/connection ---

class _FakeCursor:
    """Mimics enough of psycopg2's cursor for the checking-side functions
    (list_price_sources_due, list_price_sources_for_products,
    record_price_check) AND the discovery-side functions
    (list_active_price_sites, fetch_products_to_discover,
    mark_product_price_discovery_searched, insert_price_source_candidates).
    Several queries share a column prefix (see app.py) so they're
    disambiguated by a substring check, same technique
    test_admin_api_service.py's FakeCursor already uses elsewhere in this
    project for overlapping query prefixes."""

    def __init__(self, sources=None, sites=None, products=None, known_candidate_keys=None, bowlerdepot_matches=None):
        self.sources = sources or []  # list of dicts: id, product_id, product_url, css_selector, is_active, is_site_active, last_checked_at, fetch_method, external_product_id
        self.sites = sites or []  # list of dicts: id, name, search_url_template, result_link_selector, default_css_selector, fetch_method, api_provider, base_url
        self.products = products or []  # list of dicts: id, name, brand_name
        self.known_candidate_keys = known_candidate_keys or set()  # set of (product_id, price_site_id, product_url)
        # list of dicts: product_id, external_product_id, match_status --
        # bowlerdepot_products rows list_bowlerdepot_matches reads
        # (016_price_tracking_bigcommerce.sql).
        self.bowlerdepot_matches = bowlerdepot_matches or []
        self.executed = []
        self.description = None
        self.rowcount = 0
        self._rows = []
        self.history_inserts = []  # list of (price_source_id, price, raw_price_text, error, cost_price, in_stock)
        self.last_checked_at_updates = []  # list of price_source_id
        self.last_discovery_marked = []  # list of product_id

    def execute(self, query, params=None):
        params = params or []
        q = " ".join(query.split())
        self.executed.append((q, params))

        if q.startswith("select pps.id, pps.product_id, pps.product_url"):
            active = [s for s in self.sources if s.get("is_active", True) and s.get("is_site_active", True)]
            if "pps.product_id = any(%s)" in q:
                (product_ids,) = params
                matched = [s for s in active if s["product_id"] in product_ids]
                matched.sort(key=lambda s: (s["product_id"], s["id"]))
                self._rows = [
                    (s["id"], s["product_id"], s["product_url"], s["css_selector"],
                     s.get("fetch_method", "scrape"), s.get("external_product_id"))
                    for s in matched
                ]
            else:
                (limit,) = params
                ordered = sorted(active, key=lambda s: (s.get("last_checked_at") is not None, s.get("last_checked_at"), s["id"]))
                self._rows = [
                    (s["id"], s["product_id"], s["product_url"], s["css_selector"],
                     s.get("fetch_method", "scrape"), s.get("external_product_id"))
                    for s in ordered[:limit]
                ]
            self.description = [
                ("id",), ("product_id",), ("product_url",), ("css_selector",),
                ("fetch_method",), ("external_product_id",),
            ]

        elif q.startswith("insert into product_price_history"):
            self.history_inserts.append(tuple(params))

        elif q.startswith("update product_price_sources set last_checked_at"):
            (price_source_id,) = params
            self.last_checked_at_updates.append(price_source_id)

        elif q.startswith("select id, name, search_url_template, result_link_selector, default_css_selector,"):
            ordered = sorted(self.sites, key=lambda s: s["name"])
            self._rows = [
                (
                    s["id"], s["name"], s.get("search_url_template"), s.get("result_link_selector"),
                    s.get("default_css_selector"), s.get("fetch_method", "scrape"), s.get("api_provider"),
                    s.get("base_url"),
                )
                for s in ordered
            ]
            self.description = [
                ("id",), ("name",), ("search_url_template",), ("result_link_selector",), ("default_css_selector",),
                ("fetch_method",), ("api_provider",), ("base_url",),
            ]

        elif q.startswith("select p.id, p.name, b.name as brand_name"):
            self.description = [("id",), ("name",), ("brand_name",)]
            self._rows = [(p["id"], p["name"], p["brand_name"]) for p in self.products]

        elif q.startswith("update products set last_price_discovery_at"):
            (product_id,) = params
            self.last_discovery_marked.append(product_id)

        elif q.startswith("insert into product_price_sources"):
            product_id, price_site_id, product_url = params[0], params[1], params[2]
            key = (product_id, price_site_id, product_url)
            if key in self.known_candidate_keys:
                self.rowcount = 0
            else:
                self.known_candidate_keys.add(key)
                self.rowcount = 1

        elif q.startswith("select product_id, bigcommerce_product_id, match_status from bowlerdepot_products"):
            self.description = [("product_id",), ("external_product_id",), ("match_status",)]
            self._rows = [(m["product_id"], m["external_product_id"], m["match_status"]) for m in self.bowlerdepot_matches]

        else:
            raise NotImplementedError(f"FakeCursor doesn't support: {q}")

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, sources=None, sites=None, products=None, known_candidate_keys=None, bowlerdepot_matches=None):
        self._cursor = _FakeCursor(sources, sites, products, known_candidate_keys, bowlerdepot_matches)
        self.committed = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def _sample_sources():
    return [
        {"id": "src-1", "product_id": "prod-1", "product_url": "https://a.example/p1",
         "css_selector": ".price", "is_active": True, "is_site_active": True, "last_checked_at": "2026-08-01"},
        {"id": "src-2", "product_id": "prod-1", "product_url": "https://b.example/p1",
         "css_selector": ".cost", "is_active": True, "is_site_active": True, "last_checked_at": None},
        {"id": "src-3", "product_id": "prod-2", "product_url": "https://a.example/p2",
         "css_selector": ".price", "is_active": True, "is_site_active": True, "last_checked_at": "2026-08-05"},
        {"id": "src-4", "product_id": "prod-2", "product_url": "https://a.example/p2-inactive",
         "css_selector": ".price", "is_active": False, "is_site_active": True, "last_checked_at": None},
    ]


def test_list_price_sources_due_orders_never_checked_first():
    conn = _FakeConn(_sample_sources())
    result = app.list_price_sources_due(conn, limit=10)
    ids = [r["id"] for r in result]
    # src-2 (never checked) sorts first, then src-1 (2026-08-01), then
    # src-3 (2026-08-05) -- src-4 excluded (is_active=False).
    assert ids == ["src-2", "src-1", "src-3"]


def test_list_price_sources_due_respects_limit():
    conn = _FakeConn(_sample_sources())
    result = app.list_price_sources_due(conn, limit=1)
    assert len(result) == 1
    assert result[0]["id"] == "src-2"


def test_list_price_sources_due_excludes_inactive_sources():
    conn = _FakeConn(_sample_sources())
    result = app.list_price_sources_due(conn, limit=10)
    assert all(r["id"] != "src-4" for r in result)


def test_list_price_sources_for_products_scopes_by_product_id():
    conn = _FakeConn(_sample_sources())
    result = app.list_price_sources_for_products(conn, ["prod-1"])
    ids = sorted(r["id"] for r in result)
    assert ids == ["src-1", "src-2"]


def test_list_price_sources_for_products_empty_list_returns_empty_without_query():
    conn = _FakeConn(_sample_sources())
    result = app.list_price_sources_for_products(conn, [])
    assert result == []
    assert conn._cursor.executed == []  # never even touched the DB


def test_record_price_check_writes_history_and_bumps_last_checked_at():
    conn = _FakeConn()
    app.record_price_check(conn, "src-1", {"price": 99.99, "raw_price_text": "$99.99", "error": None})

    assert conn._cursor.history_inserts == [("src-1", 99.99, "$99.99", None, None, None)]
    assert conn._cursor.last_checked_at_updates == ["src-1"]
    assert conn.committed is True


def test_record_price_check_writes_history_row_even_on_failure():
    # The whole point of the always-write-a-row design (see app.py's
    # module docstring) -- a failed check must still be visible, not
    # silently dropped.
    conn = _FakeConn()
    app.record_price_check(conn, "src-1", {"price": None, "raw_price_text": None, "error": "fetch failed: timeout"})

    assert conn._cursor.history_inserts == [("src-1", None, None, "fetch failed: timeout", None, None)]
    assert conn._cursor.last_checked_at_updates == ["src-1"]


def test_record_price_check_writes_cost_price_and_in_stock_when_present():
    # 016_price_tracking_bigcommerce.sql -- a BigCommerce/'api' check
    # result carries cost_price/in_stock alongside price; a scrape-path
    # result dict never has these keys at all, so result.get(...) must
    # default to None for the two tests above to keep passing unchanged.
    conn = _FakeConn()
    app.record_price_check(conn, "src-1", {
        "price": 149.99, "raw_price_text": None, "error": None, "cost_price": 80.0, "in_stock": True,
    })

    assert conn._cursor.history_inserts == [("src-1", 149.99, None, None, 80.0, True)]


def test_check_sources_tolerates_per_source_failure_and_counts_correctly(monkeypatch):
    def _fake_check(source, session=None):
        if source["id"] == "src-bad":
            return {"price": None, "raw_price_text": None, "error": "fetch failed: boom"}
        return {"price": 42.00, "raw_price_text": "$42.00", "error": None}

    monkeypatch.setattr(app, "check_price_source", _fake_check)
    conn = _FakeConn()
    sources = [
        {"id": "src-good-1", "product_id": "p1", "product_url": "u1", "css_selector": ".price"},
        {"id": "src-bad", "product_id": "p1", "product_url": "u2", "css_selector": ".price"},
        {"id": "src-good-2", "product_id": "p2", "product_url": "u3", "css_selector": ".price"},
    ]

    result = app.check_sources(conn, sources)

    assert result == {"sources_checked": 3, "succeeded": 2, "failed": 1}
    assert len(conn._cursor.history_inserts) == 3  # every source got a row, including the failed one


# --- handler: job-shape dispatch ---

def test_handler_product_ids_shape_scopes_to_those_products(monkeypatch):
    calls = {}

    def _fake_for_products(conn, ids):
        calls["for_products"] = ids
        return [{"id": "s1"}]

    def _fake_due(conn, limit):
        calls["due_called"] = True
        return []

    monkeypatch.setattr(app, "get_requests_session", lambda: "fake-session")
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())
    monkeypatch.setattr(app, "list_price_sources_for_products", _fake_for_products)
    monkeypatch.setattr(app, "list_price_sources_due", _fake_due)
    monkeypatch.setattr(app, "check_sources", lambda conn, sources, session=None: {"sources_checked": len(sources), "succeeded": len(sources), "failed": 0})

    result = app.handler({"product_ids": ["prod-1"]}, None)
    body = json.loads(result["body"])

    assert calls["for_products"] == ["prod-1"]
    assert "due_called" not in calls  # batch path never touched
    assert body["sources_checked"] == 1


def test_handler_empty_event_runs_batch_with_default_limit(monkeypatch):
    calls = {}

    def _fake_due(conn, limit):
        calls["limit"] = limit
        return []

    monkeypatch.setattr(app, "get_requests_session", lambda: "fake-session")
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())
    monkeypatch.setattr(app, "list_price_sources_due", _fake_due)
    monkeypatch.setattr(app, "check_sources", lambda conn, sources, session=None: {"sources_checked": 0, "succeeded": 0, "failed": 0})

    app.handler({}, None)

    assert calls["limit"] == app.DEFAULT_PRICE_CHECK_LIMIT


def test_handler_explicit_limit_overrides_default(monkeypatch):
    calls = {}

    def _fake_due(conn, limit):
        calls["limit"] = limit
        return []

    monkeypatch.setattr(app, "get_requests_session", lambda: "fake-session")
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())
    monkeypatch.setattr(app, "list_price_sources_due", _fake_due)
    monkeypatch.setattr(app, "check_sources", lambda conn, sources, session=None: {"sources_checked": 0, "succeeded": 0, "failed": 0})

    app.handler({"limit": 5}, None)

    assert calls["limit"] == 5


def test_handler_closes_connection_even_on_error(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(app, "get_requests_session", lambda: "fake-session")
    monkeypatch.setattr(app, "get_db_connection", lambda: conn)
    monkeypatch.setattr(app, "list_price_sources_due", lambda c, limit: (_ for _ in ()).throw(RuntimeError("boom")))

    try:
        app.handler({}, None)
        assert False, "expected RuntimeError to propagate"
    except RuntimeError:
        pass

    assert conn.closed is True


# --- significant_tokens / score_match / build_search_query: pure, no DB,
# no network. Identical logic to video_discovery's own versions -- see
# app.py's comment on why they're duplicated rather than shared. ---

def test_significant_tokens_strips_stopwords_and_lowercases():
    assert app.significant_tokens("Storm Bowling Ball") == {"storm"}


def test_significant_tokens_empty_for_blank_name():
    assert app.significant_tokens("") == set()
    assert app.significant_tokens(None) == set()


def test_score_match_high_when_brand_and_product_token_both_present():
    assert app.score_match("Storm Absolute Bowling Ball Review 2026", "Storm", "Absolute") == "high"


def test_score_match_low_when_brand_missing():
    assert app.score_match("Absolute Bowling Ball Review", "Storm", "Absolute") == "low"


def test_score_match_low_when_title_blank():
    assert app.score_match("", "Storm", "Absolute") == "low"


def test_build_search_query_combines_brand_and_product():
    assert app.build_search_query("Storm", "Absolute") == "Storm Absolute"


# --- parse_search_results / search_site_for_product: bs4 selector +
# urljoin, small hand-written HTML (same "no real network access in this
# sandbox" honesty note as extract_price's tests above). ---

def test_parse_search_results_extracts_url_and_title():
    html = '''
        <div class="results">
            <a class="product-link" href="/products/storm-absolute">Storm Absolute</a>
            <a class="product-link" href="/products/storm-fury">Storm Fury</a>
        </div>
    '''
    results = app.parse_search_results(html, ".product-link", "https://example.com/search", max_results=5)
    assert results == [
        {"product_url": "https://example.com/products/storm-absolute", "title": "Storm Absolute"},
        {"product_url": "https://example.com/products/storm-fury", "title": "Storm Fury"},
    ]


def test_parse_search_results_respects_max_results():
    html = '<a class="product-link" href="/p1">P1</a><a class="product-link" href="/p2">P2</a>'
    results = app.parse_search_results(html, ".product-link", "https://example.com/search", max_results=1)
    assert len(results) == 1


def test_parse_search_results_skips_missing_href():
    html = '<a class="product-link">No href</a><a class="product-link" href="/p1">P1</a>'
    results = app.parse_search_results(html, ".product-link", "https://example.com/search", max_results=5)
    assert results == [{"product_url": "https://example.com/p1", "title": "P1"}]


def test_parse_search_results_dedupes_same_resolved_url():
    """A site's markup occasionally wraps both a thumbnail and a title in
    separate <a> tags pointing at the identical product page."""
    html = '<a class="product-link" href="/p1"><img></a><a class="product-link" href="/p1">P1 Title</a>'
    results = app.parse_search_results(html, ".product-link", "https://example.com/search", max_results=5)
    assert len(results) == 1
    assert results[0]["product_url"] == "https://example.com/p1"


def test_search_site_for_product_url_encodes_query_and_fetches(monkeypatch):
    captured = {}

    def _fake_fetch_page(url, session=None, timeout=app.DEFAULT_FETCH_TIMEOUT_SECONDS):
        captured["url"] = url
        return '<a class="product-link" href="/p1">Storm Absolute</a>'

    monkeypatch.setattr(app, "fetch_page", _fake_fetch_page)
    site = {
        "search_url_template": "https://example.com/search?q={query}",
        "result_link_selector": ".product-link",
    }
    results = app.search_site_for_product(site, "Storm Absolute Bowling Ball", max_results=5)

    assert captured["url"] == "https://example.com/search?q=Storm+Absolute+Bowling+Ball"
    assert results == [{"product_url": "https://example.com/p1", "title": "Storm Absolute"}]


# --- Discovery-side DB-facing functions ---

def _sample_sites():
    return [
        {"id": "site-1", "name": "Bowling.com", "search_url_template": "https://bowling.com/search?q={query}",
         "result_link_selector": ".product-link", "default_css_selector": ".price"},
        {"id": "site-2", "name": "BowlingBall.com", "search_url_template": "https://bowlingball.com/search?q={query}",
         "result_link_selector": ".item-link", "default_css_selector": "[itemprop=price]"},
    ]


def test_list_active_price_sites_orders_by_name():
    conn = _FakeConn(sites=_sample_sites())
    result = app.list_active_price_sites(conn)
    assert [s["name"] for s in result] == ["Bowling.com", "BowlingBall.com"]
    assert result[0]["search_url_template"] == "https://bowling.com/search?q={query}"


def test_fetch_products_to_discover_defaults_to_current_status_only():
    conn = _FakeConn(products=[{"id": "p1", "name": "Absolute", "brand_name": "Storm"}])
    products = app.fetch_products_to_discover(conn, {}, max_products=100)

    assert products == [{"id": "p1", "name": "Absolute", "brand_name": "Storm"}]
    query, params = conn.cursor().executed[0]
    assert "p.status = 'current'" in query
    assert "order by p.last_price_discovery_at asc nulls first, p.id asc limit %s" in query
    assert params[-1] == 100


def test_fetch_products_to_discover_product_ids_scope():
    conn = _FakeConn(products=[])
    app.fetch_products_to_discover(conn, {"product_ids": ["p1"]}, max_products=100)

    query, _ = conn.cursor().executed[0]
    assert "p.id = any(%s::uuid[])" in query
    assert "order by p.id asc limit %s" in query
    assert "last_price_discovery_at" not in query


def test_fetch_products_to_discover_brand_id_scope():
    conn = _FakeConn(products=[])
    app.fetch_products_to_discover(conn, {"brand_id": "brand-1"}, max_products=100)

    query, params = conn.cursor().executed[0]
    assert "p.brand_id = %s" in query
    assert "brand-1" in params


def test_mark_product_price_discovery_searched_writes_and_commits():
    conn = _FakeConn()
    app.mark_product_price_discovery_searched(conn, "prod-1")

    assert conn.cursor().last_discovery_marked == ["prod-1"]
    assert conn.committed is True


def test_insert_price_source_candidates_counts_new_rows():
    conn = _FakeConn()
    candidates = [
        {"product_url": "https://a.example/p1", "match_confidence": "high"},
        {"product_url": "https://a.example/p2", "match_confidence": "low"},
    ]
    inserted = app.insert_price_source_candidates(conn, "prod-1", "site-1", "Storm Absolute", candidates)
    assert inserted == 2
    assert conn.committed is True


def test_insert_price_source_candidates_idempotent_against_known_url():
    known = {("prod-1", "site-1", "https://a.example/p1")}
    conn = _FakeConn(known_candidate_keys=known)
    candidates = [{"product_url": "https://a.example/p1", "match_confidence": "high"}]
    inserted = app.insert_price_source_candidates(conn, "prod-1", "site-1", "Storm Absolute", candidates)
    assert inserted == 0  # already known -- ON CONFLICT DO NOTHING

    query, params = conn.cursor().executed[0]
    assert "status" in query and "'pending'" in query
    # source defaults to 'site_search' as a bound param now (016_price_
    # tracking_bigcommerce.sql added a source= parameter so bigcommerce_api
    # candidates can share this same insert), not a hardcoded SQL literal.
    assert params == ("prod-1", "site-1", "https://a.example/p1", "Storm Absolute", "high", None, "site_search")


def test_insert_price_source_candidates_default_source_is_site_search():
    conn = _FakeConn()
    candidates = [{"product_url": "https://a.example/p1", "match_confidence": "high"}]
    app.insert_price_source_candidates(conn, "prod-1", "site-1", "Storm Absolute", candidates)
    _, params = conn.cursor().executed[0]
    assert params[-1] == "site_search"


def test_insert_price_source_candidates_accepts_bigcommerce_api_source_and_external_id():
    # discover_bigcommerce_candidates' own call shape (016_price_tracking_
    # bigcommerce.sql) -- source='bigcommerce_api' and a real
    # external_product_id, not the site_search defaults.
    conn = _FakeConn()
    candidates = [{
        "product_url": "https://www.bowlerdepot.com/storm-alpha-crux/",
        "match_confidence": "high",
        "external_product_id": "100",
    }]
    inserted = app.insert_price_source_candidates(
        conn, "prod-1", "site-bd", "bowlerdepot_products match", candidates, source="bigcommerce_api",
    )
    assert inserted == 1
    _, params = conn.cursor().executed[0]
    assert params == (
        "prod-1", "site-bd", "https://www.bowlerdepot.com/storm-alpha-crux/",
        "bowlerdepot_products match", "high", "100", "bigcommerce_api",
    )


# --- discover_price_sources: orchestration, tolerates per-site failures ---

def test_discover_price_sources_inserts_candidates_across_products_and_sites(monkeypatch):
    conn = _FakeConn(
        products=[{"id": "prod-1", "name": "Absolute", "brand_name": "Storm"}],
        sites=_sample_sites(),
    )

    def _fake_search(site, query, session=None, max_results=app.DEFAULT_MAX_RESULTS_PER_SITE_SEARCH):
        return [{"product_url": f"https://{site['id']}.example/p1", "title": "Storm Absolute"}]

    monkeypatch.setattr(app, "search_site_for_product", _fake_search)

    result = app.discover_price_sources(conn, {}, session=None)

    assert result == {"products_searched": 1, "sites_searched": 2, "new_candidates": 2, "search_errors": 0}
    assert conn.cursor().last_discovery_marked == ["prod-1"]


def test_discover_price_sources_tolerates_per_site_search_failure(monkeypatch):
    conn = _FakeConn(
        products=[{"id": "prod-1", "name": "Absolute", "brand_name": "Storm"}],
        sites=_sample_sites(),
    )

    def _fake_search(site, query, session=None, max_results=app.DEFAULT_MAX_RESULTS_PER_SITE_SEARCH):
        if site["id"] == "site-2":
            raise ConnectionError("timed out")
        return [{"product_url": "https://site-1.example/p1", "title": "Storm Absolute"}]

    monkeypatch.setattr(app, "search_site_for_product", _fake_search)

    result = app.discover_price_sources(conn, {}, session=None)

    assert result["new_candidates"] == 1
    assert result["search_errors"] == 1
    # A search failure for one site must not stop the product from still
    # being marked as searched -- same "one bad row can't stop the batch"
    # stance check_sources already takes on the checking side.
    assert conn.cursor().last_discovery_marked == ["prod-1"]


def test_discover_price_sources_scores_match_confidence(monkeypatch):
    conn = _FakeConn(
        products=[{"id": "prod-1", "name": "Absolute", "brand_name": "Storm"}],
        sites=[_sample_sites()[0]],
    )
    captured = {}

    def _fake_search(site, query, session=None, max_results=app.DEFAULT_MAX_RESULTS_PER_SITE_SEARCH):
        return [{"product_url": "https://site-1.example/p1", "title": "Storm Absolute Bowling Ball"}]

    def _fake_insert(c, product_id, price_site_id, query, candidates):
        captured["candidates"] = candidates
        return len(candidates)

    monkeypatch.setattr(app, "search_site_for_product", _fake_search)
    monkeypatch.setattr(app, "insert_price_source_candidates", _fake_insert)

    app.discover_price_sources(conn, {}, session=None)

    assert captured["candidates"][0]["match_confidence"] == "high"


# --- handler: {"discover": true} job-shape dispatch ---

def test_handler_discover_shape_calls_discover_price_sources(monkeypatch):
    calls = {}

    def _fake_discover(conn, job, session=None):
        calls["job"] = job
        return {"products_searched": 1, "sites_searched": 2, "new_candidates": 3, "search_errors": 0}

    monkeypatch.setattr(app, "get_requests_session", lambda: "fake-session")
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())
    monkeypatch.setattr(app, "discover_price_sources", _fake_discover)
    monkeypatch.setattr(app, "list_price_sources_due", lambda conn, limit: (_ for _ in ()).throw(AssertionError("checking path must not run")))

    result = app.handler({"discover": True, "product_ids": ["prod-1"]}, None)
    body = json.loads(result["body"])

    assert calls["job"] == {"discover": True, "product_ids": ["prod-1"]}
    assert body["new_candidates"] == 3


# --- BowlerDepot/BigCommerce API source type (016_price_tracking_
# bigcommerce.sql) -- Al: "this... is a project for the same company that
# owns bowlerdepot.com... Going with the API for that one would be great
# and there are some additional data points... In stock over time and
# cost price over time." Honesty note: no real BowlerDepot store_hash/API
# token exists in this project yet (see get_bigcommerce_credentials'
# BIGCOMMERCE_SECRET_ARN and bowlerdepot_reconciliation's own module
# docstring) -- every test below either monkeypatches
# get_bigcommerce_credentials directly (orchestration-level tests, same
# level search_site_for_product's own tests monkeypatch fetch_page at)
# or feeds a small fake requests-shaped session object with hand-written
# BigCommerce-v3-shaped JSON (confirmed response contract, see
# bowlerdepot_reconciliation/app.py's own module docstring), not a real
# API response capture. ---

class _FakeBigCommerceResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _FakeBigCommerceSession:
    """Single-page-per-call fake -- good enough for these tests since none
    of them exercise pagination (total_pages > 1); pagination itself is
    exactly the same "walk total_pages, extend" loop
    bowlerdepot_reconciliation.fetch_all_products already has its own
    tests for, not re-tested here."""

    def __init__(self, products):
        self.products = products
        self.urls = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        return _FakeBigCommerceResponse({
            "data": self.products,
            "meta": {"pagination": {"total_pages": 1}},
        })


def test_determine_in_stock_none_tracking_uses_availability():
    assert app.determine_in_stock({"inventory_tracking": "none", "availability": "available"}) is True
    assert app.determine_in_stock({"inventory_tracking": "none", "availability": "disabled"}) is False


def test_determine_in_stock_none_tracking_missing_availability_is_none():
    assert app.determine_in_stock({"inventory_tracking": "none"}) is None


def test_determine_in_stock_product_or_variant_tracking_uses_inventory_level():
    assert app.determine_in_stock({"inventory_tracking": "product", "inventory_level": 5}) is True
    assert app.determine_in_stock({"inventory_tracking": "product", "inventory_level": 0}) is False
    assert app.determine_in_stock({"inventory_tracking": "variant", "inventory_level": 1}) is True


def test_determine_in_stock_unknown_or_missing_tracking_is_none():
    assert app.determine_in_stock({}) is None
    assert app.determine_in_stock({"inventory_tracking": "something_new"}) is None


def test_build_bigcommerce_products_by_id_url_uses_id_in_filter():
    url = app.build_bigcommerce_products_by_id_url("store123", [1, 2, 3])
    assert url == (
        "https://api.bigcommerce.com/stores/store123/v3/catalog/products"
        "?id:in=1,2,3&page=1&limit=250&include=custom_fields"
    )


def test_fetch_bigcommerce_products_by_ids_returns_by_id_lookup():
    session = _FakeBigCommerceSession([
        {"id": 100, "price": 149.99},
        {"id": 300, "price": 199.99},
    ])
    result = app.fetch_bigcommerce_products_by_ids("store123", "tok", [100, 300], session=session)
    assert set(result.keys()) == {"100", "300"}
    assert result["100"]["price"] == 149.99


def test_fetch_bigcommerce_products_by_ids_batches_large_id_lists():
    # MAX_BIGCOMMERCE_IDS_PER_CALL=50 -- 120 ids should mean 3 separate
    # calls, not one giant id:in= list.
    ids = list(range(120))
    session = _FakeBigCommerceSession([])
    app.fetch_bigcommerce_products_by_ids("store123", "tok", ids, session=session)
    assert len(session.urls) == 3


def test_extract_bigcommerce_price_fields_resolves_relative_url_against_base():
    product = {
        "price": 149.99, "cost_price": 80.0,
        "inventory_tracking": "none", "availability": "available",
        "custom_url": {"url": "/storm-alpha-crux/"},
    }
    fields = app.extract_bigcommerce_price_fields(product, base_url="https://www.bowlerdepot.com")
    assert fields == {
        "price": 149.99, "cost_price": 80.0, "in_stock": True,
        "product_url": "https://www.bowlerdepot.com/storm-alpha-crux/",
        "raw_price_text": None, "error": None,
    }


def test_extract_bigcommerce_price_fields_no_base_url_falls_back_to_relative_path():
    product = {"price": 10.0, "custom_url": {"url": "/p/"}}
    fields = app.extract_bigcommerce_price_fields(product, base_url=None)
    assert fields["product_url"] == "/p/"


def test_extract_bigcommerce_price_fields_missing_custom_url_is_none():
    fields = app.extract_bigcommerce_price_fields({"price": 10.0}, base_url="https://example.com")
    assert fields["product_url"] is None


def test_list_bowlerdepot_matches_shape():
    conn = _FakeConn(bowlerdepot_matches=[
        {"product_id": "p1", "external_product_id": "100", "match_status": "matched"},
        {"product_id": "p2", "external_product_id": "200", "match_status": "ambiguous"},
    ])
    result = app.list_bowlerdepot_matches(conn)
    assert result == [
        {"product_id": "p1", "external_product_id": "100", "match_status": "matched"},
        {"product_id": "p2", "external_product_id": "200", "match_status": "ambiguous"},
    ]


def test_check_bigcommerce_sources_returns_price_cost_stock_per_source(monkeypatch):
    monkeypatch.setattr(app, "get_bigcommerce_credentials", lambda: ("store123", "tok"))
    session = _FakeBigCommerceSession([
        {"id": 100, "price": 149.99, "cost_price": 80.0, "inventory_tracking": "none", "availability": "available"},
    ])
    sources = [{"id": "src-1", "external_product_id": "100"}]
    results = app.check_bigcommerce_sources(sources, session=session)
    assert results["src-1"]["price"] == 149.99
    assert results["src-1"]["cost_price"] == 80.0
    assert results["src-1"]["in_stock"] is True
    assert results["src-1"]["error"] is None


def test_check_bigcommerce_sources_missing_external_id_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(app, "get_bigcommerce_credentials", lambda: ("store123", "tok"))
    session = _FakeBigCommerceSession([])
    sources = [{"id": "src-1", "external_product_id": None}]
    results = app.check_bigcommerce_sources(sources, session=session)
    assert results["src-1"]["error"] == "no external_product_id set"


def test_check_bigcommerce_sources_product_no_longer_in_bigcommerce_is_an_error(monkeypatch):
    monkeypatch.setattr(app, "get_bigcommerce_credentials", lambda: ("store123", "tok"))
    session = _FakeBigCommerceSession([])  # empty -- id 100 was deleted/unpublished
    sources = [{"id": "src-1", "external_product_id": "100"}]
    results = app.check_bigcommerce_sources(sources, session=session)
    assert "no longer has product id 100" in results["src-1"]["error"]


def test_check_bigcommerce_sources_credentials_failure_errors_every_source_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("no secret configured")

    monkeypatch.setattr(app, "get_bigcommerce_credentials", _boom)
    sources = [{"id": "src-1", "external_product_id": "100"}, {"id": "src-2", "external_product_id": "200"}]
    results = app.check_bigcommerce_sources(sources, session=None)
    assert "BigCommerce credentials unavailable" in results["src-1"]["error"]
    assert "BigCommerce credentials unavailable" in results["src-2"]["error"]


def test_check_bigcommerce_sources_fetch_failure_errors_every_source_never_raises(monkeypatch):
    monkeypatch.setattr(app, "get_bigcommerce_credentials", lambda: ("store123", "tok"))

    def _boom(store_hash, auth_token, ids, session=None):
        raise ConnectionError("timed out")

    monkeypatch.setattr(app, "fetch_bigcommerce_products_by_ids", _boom)
    sources = [{"id": "src-1", "external_product_id": "100"}]
    results = app.check_bigcommerce_sources(sources, session=None)
    assert "BigCommerce fetch failed" in results["src-1"]["error"]


# --- check_sources: partitions scrape vs api sources (016_price_tracking_
# bigcommerce.sql) ---

def test_check_sources_routes_scrape_and_api_sources_separately(monkeypatch):
    monkeypatch.setattr(app, "check_price_source", lambda source, session=None: {
        "price": 1.0, "raw_price_text": "$1.00", "error": None,
    })
    monkeypatch.setattr(app, "check_bigcommerce_sources", lambda sources, session=None: {
        s["id"]: {"price": 2.0, "raw_price_text": None, "error": None, "cost_price": 1.5, "in_stock": True}
        for s in sources
    })
    conn = _FakeConn()
    sources = [
        {"id": "sc-1", "fetch_method": "scrape", "product_url": "https://a.example", "css_selector": ".p"},
        {"id": "ap-1", "fetch_method": "api", "external_product_id": "100"},
    ]
    result = app.check_sources(conn, sources)
    assert result == {"sources_checked": 2, "succeeded": 2, "failed": 0}
    assert conn.cursor().history_inserts == [
        ("sc-1", 1.0, "$1.00", None, None, None),
        ("ap-1", 2.0, None, None, 1.5, True),
    ]


def test_check_sources_missing_fetch_method_defaults_to_scrape(monkeypatch):
    # A source dict without a fetch_method key at all (e.g. an older
    # in-memory fixture, or list_price_sources_due's own row shape before
    # this migration) must still route through the scrape path, not
    # silently vanish from either bucket.
    conn = _FakeConn()
    calls = []

    def _fake_check(source, session=None):
        calls.append(source["id"])
        return {"price": 1.0, "raw_price_text": "$1.00", "error": None}

    monkeypatch.setattr(app, "check_price_source", _fake_check)
    result = app.check_sources(conn, [{"id": "src-1", "product_url": "u", "css_selector": ".p"}])
    assert calls == ["src-1"]
    assert result["sources_checked"] == 1


def test_check_sources_no_api_sources_never_calls_check_bigcommerce_sources(monkeypatch):
    monkeypatch.setattr(app, "check_price_source", lambda source, session=None: {
        "price": 1.0, "raw_price_text": "$1.00", "error": None,
    })
    monkeypatch.setattr(app, "check_bigcommerce_sources",
                         lambda sources, session=None: (_ for _ in ()).throw(AssertionError("must not be called")))
    conn = _FakeConn()
    result = app.check_sources(conn, [{"id": "sc-1", "fetch_method": "scrape", "product_url": "u", "css_selector": ".p"}])
    assert result["sources_checked"] == 1


# --- discover_bigcommerce_candidates / discover_price_sources with an
# 'api' price_sites row (016_price_tracking_bigcommerce.sql) ---

def test_discover_bigcommerce_candidates_inserts_high_and_low_confidence(monkeypatch):
    monkeypatch.setattr(app, "get_bigcommerce_credentials", lambda: ("store123", "tok"))
    conn = _FakeConn(bowlerdepot_matches=[
        {"product_id": "p1", "external_product_id": "100", "match_status": "matched"},
        {"product_id": "p2", "external_product_id": "200", "match_status": "ambiguous"},
    ])
    session = _FakeBigCommerceSession([
        {"id": 100, "custom_url": {"url": "/ball-a/"}},
        {"id": 200, "custom_url": {"url": "/ball-b/"}},
    ])
    site = {"id": "site-bd", "name": "BowlerDepot", "base_url": "https://www.bowlerdepot.com"}
    result = app.discover_bigcommerce_candidates(conn, site, {"p1", "p2"}, session=session)
    assert result == {"inserted": 2, "errors": 0}

    inserts = [e for e in conn.cursor().executed if e[0].startswith("insert into product_price_sources")]
    assert len(inserts) == 2
    confidences = sorted(params[4] for _, params in inserts)
    assert confidences == ["high", "low"]


def test_discover_bigcommerce_candidates_scopes_to_products_in_scope(monkeypatch):
    monkeypatch.setattr(app, "get_bigcommerce_credentials", lambda: ("store123", "tok"))
    captured = {}

    def _fake_fetch(store_hash, auth_token, ids, session=None):
        captured["ids"] = ids
        return {"100": {"id": 100, "custom_url": {"url": "/ball-a/"}}}

    monkeypatch.setattr(app, "fetch_bigcommerce_products_by_ids", _fake_fetch)
    conn = _FakeConn(bowlerdepot_matches=[
        {"product_id": "p1", "external_product_id": "100", "match_status": "matched"},
        {"product_id": "p-out-of-scope", "external_product_id": "999", "match_status": "matched"},
    ])
    site = {"id": "site-bd", "name": "BowlerDepot", "base_url": "https://www.bowlerdepot.com"}
    # Only p1 in scope -- p-out-of-scope's match must never even be sent
    # to fetch_bigcommerce_products_by_ids.
    result = app.discover_bigcommerce_candidates(conn, site, {"p1"}, session=None)
    assert captured["ids"] == ["100"]
    assert result == {"inserted": 1, "errors": 0}


def test_discover_bigcommerce_candidates_missing_bigcommerce_product_is_an_error(monkeypatch):
    monkeypatch.setattr(app, "get_bigcommerce_credentials", lambda: ("store123", "tok"))
    conn = _FakeConn(bowlerdepot_matches=[
        {"product_id": "p1", "external_product_id": "100", "match_status": "matched"},
    ])
    session = _FakeBigCommerceSession([])  # BigCommerce no longer has id 100
    site = {"id": "site-bd", "name": "BowlerDepot", "base_url": "https://www.bowlerdepot.com"}
    result = app.discover_bigcommerce_candidates(conn, site, {"p1"}, session=session)
    assert result == {"inserted": 0, "errors": 1}


def test_discover_bigcommerce_candidates_no_matches_in_scope_short_circuits():
    conn = _FakeConn(bowlerdepot_matches=[])
    site = {"id": "site-bd", "name": "BowlerDepot"}
    result = app.discover_bigcommerce_candidates(conn, site, {"p1"}, session=None)
    assert result == {"inserted": 0, "errors": 0}
    # No BigCommerce credentials/fetch call and no insert -- just the one
    # read of bowlerdepot_products, then an early return once it's clear
    # there's nothing in scope to look up.
    assert len(conn.cursor().executed) == 1
    assert conn.cursor().executed[0][0].startswith("select product_id, bigcommerce_product_id, match_status")


def test_discover_price_sources_handles_mixed_scrape_and_api_sites(monkeypatch):
    conn = _FakeConn(
        products=[{"id": "p1", "name": "Absolute", "brand_name": "Storm"}],
        sites=[
            {"id": "site-1", "name": "Bowling.com", "search_url_template": "https://bowling.com/search?q={query}",
             "result_link_selector": ".product-link", "default_css_selector": ".price", "fetch_method": "scrape"},
            {"id": "site-bd", "name": "BowlerDepot", "fetch_method": "api", "api_provider": "bigcommerce",
             "base_url": "https://www.bowlerdepot.com"},
        ],
        bowlerdepot_matches=[{"product_id": "p1", "external_product_id": "100", "match_status": "matched"}],
    )

    def _fake_search(site, query, session=None, max_results=app.DEFAULT_MAX_RESULTS_PER_SITE_SEARCH):
        return [{"product_url": "https://bowling.com/p1", "title": "Storm Absolute"}]

    monkeypatch.setattr(app, "search_site_for_product", _fake_search)
    monkeypatch.setattr(app, "get_bigcommerce_credentials", lambda: ("store123", "tok"))
    monkeypatch.setattr(app, "fetch_bigcommerce_products_by_ids", lambda store_hash, auth_token, ids, session=None: {
        "100": {"id": 100, "custom_url": {"url": "/storm-absolute/"}},
    })

    result = app.discover_price_sources(conn, {}, session=None)

    assert result["new_candidates"] == 2  # 1 scrape candidate + 1 bigcommerce candidate
    assert result["search_errors"] == 0
    assert result["sites_searched"] == 2


def test_discover_price_sources_bigcommerce_failure_does_not_block_scrape_sites(monkeypatch):
    conn = _FakeConn(
        products=[{"id": "p1", "name": "Absolute", "brand_name": "Storm"}],
        sites=[
            {"id": "site-1", "name": "Bowling.com", "search_url_template": "https://bowling.com/search?q={query}",
             "result_link_selector": ".product-link", "default_css_selector": ".price", "fetch_method": "scrape"},
            {"id": "site-bd", "name": "BowlerDepot", "fetch_method": "api", "api_provider": "bigcommerce"},
        ],
        bowlerdepot_matches=[{"product_id": "p1", "external_product_id": "100", "match_status": "matched"}],
    )

    def _fake_search(site, query, session=None, max_results=app.DEFAULT_MAX_RESULTS_PER_SITE_SEARCH):
        return [{"product_url": "https://bowling.com/p1", "title": "Storm Absolute"}]

    def _boom():
        raise RuntimeError("no secret configured")

    monkeypatch.setattr(app, "search_site_for_product", _fake_search)
    monkeypatch.setattr(app, "get_bigcommerce_credentials", _boom)

    result = app.discover_price_sources(conn, {}, session=None)

    assert result["new_candidates"] == 1  # the scrape candidate still landed
    assert result["search_errors"] == 1  # the 1 in-scope bigcommerce match counted as an error
    assert conn.cursor().last_discovery_marked == ["p1"]  # scrape loop still ran to completion


# --- list_active_price_sites / list_price_sources_due /
# list_price_sources_for_products expose fetch_method (016_price_
# tracking_bigcommerce.sql) ---

def test_list_active_price_sites_includes_fetch_method_api_provider_base_url():
    conn = _FakeConn(sites=[
        {"id": "site-bd", "name": "BowlerDepot", "search_url_template": None, "result_link_selector": None,
         "default_css_selector": None, "fetch_method": "api", "api_provider": "bigcommerce",
         "base_url": "https://www.bowlerdepot.com"},
    ])
    result = app.list_active_price_sites(conn)
    assert result[0]["fetch_method"] == "api"
    assert result[0]["api_provider"] == "bigcommerce"
    assert result[0]["base_url"] == "https://www.bowlerdepot.com"


def test_list_active_price_sites_defaults_fetch_method_for_older_fixture_rows():
    conn = _FakeConn(sites=_sample_sites())  # no fetch_method key at all
    result = app.list_active_price_sites(conn)
    assert all(s["fetch_method"] == "scrape" for s in result)


def test_list_price_sources_due_includes_fetch_method_and_external_product_id():
    sources = _sample_sources()
    sources[0]["fetch_method"] = "api"  # src-1
    sources[0]["external_product_id"] = "100"
    conn = _FakeConn(sources)
    result = app.list_price_sources_due(conn, limit=10)
    by_id = {r["id"]: r for r in result}
    assert by_id["src-1"]["fetch_method"] == "api"
    assert by_id["src-1"]["external_product_id"] == "100"
    assert by_id["src-2"]["fetch_method"] == "scrape"  # no fetch_method key -- defaults
    assert by_id["src-2"]["external_product_id"] is None


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
