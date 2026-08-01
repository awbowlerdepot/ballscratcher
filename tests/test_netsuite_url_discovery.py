"""
Tests for src/netsuite_url_discovery/app.py, run against
tests/fixtures/motiv_balls_index.html and motiv_retired_balls_index.html
(small real-link excerpts -- see each fixture's header comment).
Manual-runner pattern, run standalone via
`python3 tests/test_netsuite_url_discovery.py`.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "netsuite_url_discovery"))

import app  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

CURRENT_URL = "https://www.motivbowling.com/products/balls/"
RETIRED_URL = "https://www.motivbowling.com/products/balls/retired-balls/"

JACKAL_ONYX = "https://www.motivbowling.com/n_782172039773743965"
ASCEND = "https://www.motivbowling.com/n_760032766635683491"
RAPTOR_REIGN = "https://www.motivbowling.com/n_767678396680222492"
SOME_RETIRED_BALL = "https://www.motivbowling.com/n_618855082571878269"


def _current_html():
    return (FIXTURES / "motiv_balls_index.html").read_text()


def _retired_html():
    return (FIXTURES / "motiv_retired_balls_index.html").read_text()


def test_parse_category_page_finds_real_product_links():
    urls = app.parse_category_page(_current_html(), CURRENT_URL)
    assert urls == {JACKAL_ONYX, ASCEND}


def test_parse_category_page_dot_relative_href_resolves_to_root_not_nested_path():
    """Real bug found via this deploy's first live smoke test:
    href="./n_1094" on the retired-balls page, if resolved with urljoin
    against that page's own URL, produces the broken
    https://www.motivbowling.com/products/balls/retired-balls/n_1094
    (confirmed real: this 404s) instead of the real, working
    https://www.motivbowling.com/n_1094 (confirmed real: this redirects
    to the canonical slug page). Deliberately uses a category URL nested
    two directories deep to make sure a regression back to path-relative
    resolution would be caught -- a shallow base_url wouldn't expose it."""
    html = '<a href="./n_1094">Villain</a>'
    deeply_nested_url = "https://www.motivbowling.com/products/balls/retired-balls/"
    urls = app.parse_category_page(html, deeply_nested_url)
    assert urls == {"https://www.motivbowling.com/n_1094"}
    assert "https://www.motivbowling.com/products/balls/retired-balls/n_1094" not in urls


def test_parse_category_page_excludes_non_product_nav_link():
    """The "./products/balls/" filter nav link is real (seen on the live
    page) but must NOT match PRODUCT_LINK_RE -- only genuine /n_<id> links
    are products."""
    urls = app.parse_category_page(_current_html(), CURRENT_URL)
    assert CURRENT_URL not in urls
    assert "https://www.motivbowling.com/products/balls/" not in urls


def test_parse_category_page_retired_finds_all_three_links():
    urls = app.parse_category_page(_retired_html(), RETIRED_URL)
    assert urls == {JACKAL_ONYX, RAPTOR_REIGN, SOME_RETIRED_BALL}


def test_build_entries_current_wins_on_real_observed_overlap():
    """Jackal Onyx's id genuinely appeared on both the current and retired
    index pages this session (see fixture header comments) -- current
    must win, per build_entries()'s documented tie-break."""
    current = app.parse_category_page(_current_html(), CURRENT_URL)
    retired = app.parse_category_page(_retired_html(), RETIRED_URL)
    entries = app.build_entries(current, retired)

    by_url = {e["url"]: e["status"] for e in entries}
    assert by_url[JACKAL_ONYX] == "current"
    assert by_url[ASCEND] == "current"
    assert by_url[RAPTOR_REIGN] == "retired"
    assert by_url[SOME_RETIRED_BALL] == "retired"
    assert len(entries) == 4  # Jackal Onyx counted once, not twice


class _FakeCursor:
    def __init__(self, existing_urls):
        self.existing_urls = existing_urls
        self.executed = []
        self._last_result = None

    def execute(self, query, params=None):
        self.executed.append((" ".join(query.split()), params))
        if query.strip().startswith("select id from discovered_urls"):
            url = params[0]
            self._last_result = (1,) if url in self.existing_urls else None
        else:
            self._last_result = None

    def fetchone(self):
        return self._last_result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, existing_urls):
        self._cursor = _FakeCursor(existing_urls)
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


def test_diff_against_known_new_vs_unchanged():
    conn = _FakeConn(existing_urls={RAPTOR_REIGN})
    entries = [
        {"url": JACKAL_ONYX, "status": "current"},
        {"url": RAPTOR_REIGN, "status": "retired"},
    ]
    diff = app.diff_against_known(conn, "brand-123", entries)

    assert [e["url"] for e in diff["new"]] == [JACKAL_ONYX]
    assert [e["url"] for e in diff["unchanged"]] == [RAPTOR_REIGN]
    assert diff["changed"] == []
    assert conn.committed


def test_diff_against_known_inserts_with_status_path():
    conn = _FakeConn(existing_urls=set())
    entries = [{"url": JACKAL_ONYX, "status": "current"}]
    app.diff_against_known(conn, "brand-123", entries)

    insert_calls = [c for c in conn.cursor().executed if c[0].startswith("insert into discovered_urls")]
    assert len(insert_calls) == 1
    assert insert_calls[0][1] == ("brand-123", JACKAL_ONYX, "current")


def test_build_scrape_messages_includes_status():
    entries = [{"url": JACKAL_ONYX, "status": "current"}, {"url": RAPTOR_REIGN, "status": "retired"}]
    messages = app.build_scrape_messages("brand-123", entries)
    parsed = [json.loads(m) for m in messages]

    assert parsed[0] == {"url": JACKAL_ONYX, "brand_id": "brand-123", "status": "current"}
    assert parsed[1] == {"url": RAPTOR_REIGN, "brand_id": "brand-123", "status": "retired"}


class _FakeSqsClient:
    def __init__(self):
        self.batches = []

    def send_message_batch(self, QueueUrl, Entries):
        self.batches.append((QueueUrl, Entries))


def test_publish_messages_chunks_at_ten():
    sqs = _FakeSqsClient()
    messages = [f"msg-{i}" for i in range(23)]
    sent = app.publish_messages(sqs, "https://queue.example/q", messages)

    assert sent == 23
    assert len(sqs.batches) == 3  # 10 + 10 + 3
    assert len(sqs.batches[0][1]) == 10
    assert len(sqs.batches[2][1]) == 3


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS: {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")
