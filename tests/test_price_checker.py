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

    def __init__(self, sources=None, sites=None, products=None, known_candidate_keys=None):
        self.sources = sources or []  # list of dicts: id, product_id, product_url, css_selector, is_active, is_site_active, last_checked_at
        self.sites = sites or []  # list of dicts: id, name, search_url_template, result_link_selector, default_css_selector
        self.products = products or []  # list of dicts: id, name, brand_name
        self.known_candidate_keys = known_candidate_keys or set()  # set of (product_id, price_site_id, product_url)
        self.executed = []
        self.description = None
        self.rowcount = 0
        self._rows = []
        self.history_inserts = []  # list of (price_source_id, price, raw_price_text, error)
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
                self._rows = [(s["id"], s["product_id"], s["product_url"], s["css_selector"]) for s in matched]
            else:
                (limit,) = params
                ordered = sorted(active, key=lambda s: (s.get("last_checked_at") is not None, s.get("last_checked_at"), s["id"]))
                self._rows = [(s["id"], s["product_id"], s["product_url"], s["css_selector"]) for s in ordered[:limit]]
            self.description = [("id",), ("product_id",), ("product_url",), ("css_selector",)]

        elif q.startswith("insert into product_price_history"):
            self.history_inserts.append(tuple(params))

        elif q.startswith("update product_price_sources set last_checked_at"):
            (price_source_id,) = params
            self.last_checked_at_updates.append(price_source_id)

        elif q.startswith("select id, name, search_url_template, result_link_selector, default_css_selector from price_sites"):
            ordered = sorted(self.sites, key=lambda s: s["name"])
            self._rows = [
                (s["id"], s["name"], s["search_url_template"], s["result_link_selector"], s["default_css_selector"])
                for s in ordered
            ]
            self.description = [("id",), ("name",), ("search_url_template",), ("result_link_selector",), ("default_css_selector",)]

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

        else:
            raise NotImplementedError(f"FakeCursor doesn't support: {q}")

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, sources=None, sites=None, products=None, known_candidate_keys=None):
        self._cursor = _FakeCursor(sources, sites, products, known_candidate_keys)
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

    assert conn._cursor.history_inserts == [("src-1", 99.99, "$99.99", None)]
    assert conn._cursor.last_checked_at_updates == ["src-1"]
    assert conn.committed is True


def test_record_price_check_writes_history_row_even_on_failure():
    # The whole point of the always-write-a-row design (see app.py's
    # module docstring) -- a failed check must still be visible, not
    # silently dropped.
    conn = _FakeConn()
    app.record_price_check(conn, "src-1", {"price": None, "raw_price_text": None, "error": "fetch failed: timeout"})

    assert conn._cursor.history_inserts == [("src-1", None, None, "fetch failed: timeout")]
    assert conn._cursor.last_checked_at_updates == ["src-1"]


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
    assert "source" in query and "'site_search'" in query
    assert params == ("prod-1", "site-1", "https://a.example/p1", "Storm Absolute", "high")


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
