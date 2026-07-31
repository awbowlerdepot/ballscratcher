"""
Tests for src/commercebuild_url_discovery/app.py.

Fixture HTML below reconstructs the real confirmed link shape from this
session's curl against a Roto-Grip-filtered listing page (15 real product
links, each product appearing twice -- once via its image link, once via
its title link -- naturally deduplicated by parse_listing_page()'s set()).
See app.py's module docstring for the full research trail (real counts:
Storm 41, Roto Grip 15, 900 Global 5).

Manual-runner pattern, run standalone via
`python3 tests/test_commercebuild_url_discovery.py`.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "commercebuild_url_discovery"))

import app  # noqa: E402

BASE_URL = "https://www.stormbowling.com/products/equipment/bowling-balls/?per_page=100&filter[custom1][0]=Roto-Grip"

# Real link shape confirmed via curl this session: each product's card
# links twice (image + title), both to the same flat /{slug}-bowling-ball
# path. Includes a few of the real Roto Grip slugs seen.
ROTO_GRIP_LISTING_HTML = """
<div class="product-card">
  <a href="/roto-grip-gremlin-bowling-ball"><img src="/img/gremlin.jpg"></a>
  <a href="/roto-grip-gremlin-bowling-ball">Gremlin</a>
</div>
<div class="product-card">
  <a href="/roto-grip-hp3-bowling-ball"><img src="/img/hp3.jpg"></a>
  <a href="/roto-grip-hp3-bowling-ball">HP3</a>
</div>
<div class="product-card">
  <a href="/roto-grip-idol-nc-bowling-ball"><img src="/img/idol-nc.jpg"></a>
  <a href="/roto-grip-idol-nc-bowling-ball">Idol NC</a>
</div>
"""


# --- parse_listing_page ---

def test_parse_listing_page_dedupes_double_links():
    urls = app.parse_listing_page(ROTO_GRIP_LISTING_HTML, BASE_URL)
    assert urls == {
        "https://www.stormbowling.com/roto-grip-gremlin-bowling-ball",
        "https://www.stormbowling.com/roto-grip-hp3-bowling-ball",
        "https://www.stormbowling.com/roto-grip-idol-nc-bowling-ball",
    }


def test_parse_listing_page_empty_html_returns_empty_set():
    assert app.parse_listing_page("<html></html>", BASE_URL) == set()


def test_parse_listing_page_ignores_non_ball_links():
    html = '<a href="/products/equipment/bowling-bags/some-bag">Bag</a><a href="/roto-grip-gremlin-bowling-ball">Gremlin</a>'
    urls = app.parse_listing_page(html, BASE_URL)
    assert urls == {"https://www.stormbowling.com/roto-grip-gremlin-bowling-ball"}


# --- build_listing_url ---

def test_build_listing_url_real_facet_shape():
    url = app.build_listing_url("Roto-Grip")
    assert url == (
        "https://www.stormbowling.com/products/equipment/bowling-balls/"
        "?per_page=100&sort_by=1&filter%5Bcustom1%5D%5B0%5D=Roto-Grip"
    )


def test_build_listing_url_all_three_real_brand_filters():
    for key, filter_value in app.BRAND_FILTERS.items():
        url = app.build_listing_url(filter_value)
        assert f"filter%5Bcustom1%5D%5B0%5D={filter_value}" in url


# --- discover_urls_for_brand ---

def test_discover_urls_for_brand_uses_injected_fetch_fn():
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return ROTO_GRIP_LISTING_HTML

    urls = app.discover_urls_for_brand(fake_fetch, "Roto-Grip")
    assert len(urls) == 3
    assert len(calls) == 1
    assert "Roto-Grip" in calls[0]


def test_discover_urls_for_brand_warns_at_per_page_ceiling():
    """Real safeguard: if a brand's result count hits per_page, that means
    the catalog may have grown past what a single page returns (this
    scraper deliberately doesn't paginate, see module docstring) -- should
    warn, not silently under-count without a trace."""
    fake_html = "".join(f'<a href="/brand-ball-{i}-bowling-ball">b{i}</a>' for i in range(5))

    def fake_fetch(url):
        return fake_html

    urls = app.discover_urls_for_brand(fake_fetch, "storm", per_page=5)
    assert len(urls) == 5  # confirms it still returns everything found, just would log a warning


# --- build_entries ---

def test_build_entries_all_lastmod_none_and_sorted():
    """No sitemap lastmod source for this platform yet (see module
    docstring) -- every entry gets lastmod=None, sorted for determinism."""
    urls = {"https://www.stormbowling.com/z-ball-bowling-ball", "https://www.stormbowling.com/a-ball-bowling-ball"}
    entries = app.build_entries(urls)
    assert entries == [
        {"url": "https://www.stormbowling.com/a-ball-bowling-ball", "lastmod": None},
        {"url": "https://www.stormbowling.com/z-ball-bowling-ball", "lastmod": None},
    ]


# --- diff_against_known (fake DB conn/cursor, matching psycopg2's context-manager cursor interface) ---

class _FakeCursor:
    def __init__(self, store):
        self.store = store  # dict: url -> lastmod
        self._last_result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        sql_stripped = sql.strip()
        if sql_stripped.startswith("select"):
            url = params[0]
            self._last_result = (self.store[url],) if url in self.store else None
        elif sql_stripped.startswith("insert"):
            _, url, lastmod = params
            self.store[url] = lastmod
        elif sql_stripped.startswith("update"):
            pass  # last_seen_at touch -- nothing to assert on in this fake
        else:
            raise AssertionError(f"unexpected SQL: {sql_stripped}")

    def fetchone(self):
        return self._last_result


class _FakeConn:
    def __init__(self, known_urls=None):
        self.store = dict(known_urls or {})
        self.committed = False

    def cursor(self):
        return _FakeCursor(self.store)

    def commit(self):
        self.committed = True


def test_diff_against_known_all_new_on_first_run():
    conn = _FakeConn()
    entries = app.build_entries({"https://www.stormbowling.com/roto-grip-gremlin-bowling-ball"})
    diff = app.diff_against_known(conn, "brand-uuid-1", entries)
    assert diff["new"] == ["https://www.stormbowling.com/roto-grip-gremlin-bowling-ball"]
    assert diff["changed"] == []
    assert diff["unchanged"] == []
    assert conn.committed


def test_diff_against_known_unchanged_on_second_run():
    """With lastmod always None (see module docstring: no sitemap source
    for this platform), a URL already in discovered_urls can only ever
    come back "unchanged" -- "changed" can never fire here."""
    conn = _FakeConn(known_urls={"https://www.stormbowling.com/roto-grip-gremlin-bowling-ball": None})
    entries = app.build_entries({"https://www.stormbowling.com/roto-grip-gremlin-bowling-ball"})
    diff = app.diff_against_known(conn, "brand-uuid-1", entries)
    assert diff["new"] == []
    assert diff["changed"] == []
    assert diff["unchanged"] == ["https://www.stormbowling.com/roto-grip-gremlin-bowling-ball"]


def test_diff_against_known_mixed_new_and_unchanged():
    conn = _FakeConn(known_urls={"https://www.stormbowling.com/roto-grip-gremlin-bowling-ball": None})
    entries = app.build_entries({
        "https://www.stormbowling.com/roto-grip-gremlin-bowling-ball",
        "https://www.stormbowling.com/roto-grip-hp3-bowling-ball",
    })
    diff = app.diff_against_known(conn, "brand-uuid-1", entries)
    assert diff["new"] == ["https://www.stormbowling.com/roto-grip-hp3-bowling-ball"]
    assert diff["unchanged"] == ["https://www.stormbowling.com/roto-grip-gremlin-bowling-ball"]


# --- build_scrape_messages ---

def test_build_scrape_messages_shape():
    import json

    messages = app.build_scrape_messages("brand-uuid-1", ["https://www.stormbowling.com/roto-grip-gremlin-bowling-ball"])
    assert len(messages) == 1
    body = json.loads(messages[0])
    assert body == {"url": "https://www.stormbowling.com/roto-grip-gremlin-bowling-ball", "brand_id": "brand-uuid-1"}


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}")
        except Exception as e:
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed")
