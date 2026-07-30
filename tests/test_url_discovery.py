"""
Tests for the URL discovery Lambda's parsing logic, using a real sitemap
sample captured from brunswickbowling.com during architecture research
(see tests/fixtures/bowlerProducts_sitemap_sample.xml for provenance).

Deliberately does not touch the network or a real database: parse_sitemap
is a pure function, and diff_against_known is tested against a small fake
cursor rather than a live Postgres connection, so this suite runs anywhere
without AWS credentials or a DB instance.

Run with: pytest tests/test_url_discovery.py -v
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src" / "url_discovery"))

from app import (  # noqa: E402
    parse_sitemap,
    diff_against_known,
    build_scrape_messages,
    publish_messages,
)

FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "bowlerProducts_sitemap_sample.xml"


@pytest.fixture
def sitemap_bytes():
    return FIXTURE_PATH.read_bytes()


def test_parses_only_ball_urls(sitemap_bytes):
    """The fixture includes a non-ball URL (apparel) alongside real ball
    URLs -- confirms the path_pattern filter actually excludes it rather
    than the test only ever having seen ball URLs."""
    entries = parse_sitemap(sitemap_bytes)
    urls = [e["url"] for e in entries]

    assert all("/products/balls/" in u for u in urls)
    assert not any("apparel" in u for u in urls)
    assert len(entries) == 6  # 4 current + 2 retired in the fixture


def test_classifies_current_vs_retired(sitemap_bytes):
    entries = parse_sitemap(sitemap_bytes)
    by_url = {e["url"]: e for e in entries}

    current_url = "https://brunswickbowling.com/products/balls/current/crown-victory"
    retired_url = "https://brunswickbowling.com/products/balls/retired/attitude"

    assert by_url[current_url]["status"] == "current"
    assert by_url[retired_url]["status"] == "retired"


def test_captures_real_lastmod_values(sitemap_bytes):
    entries = parse_sitemap(sitemap_bytes)
    by_url = {e["url"]: e for e in entries}

    crown_victory = "https://brunswickbowling.com/products/balls/current/crown-victory"
    assert by_url[crown_victory]["lastmod"] == "2026-04-06T14:44:28-04:00"


def test_missing_lastmod_does_not_raise(sitemap_bytes):
    """The two retired URLs in the fixture have no <lastmod> element at all
    (real captured URLs, but lastmod wasn't observed for them via the
    sitemap during research -- see fixture file comments). Confirms the
    parser treats this as None rather than raising."""
    entries = parse_sitemap(sitemap_bytes)
    by_url = {e["url"]: e for e in entries}

    defender_url = "https://brunswickbowling.com/products/balls/retired/defender"
    assert by_url[defender_url]["lastmod"] is None


def test_custom_path_pattern_can_scope_to_current_only(sitemap_bytes):
    """Confirms path_pattern is actually configurable, not hardcoded --
    relevant since a future run might want current-only vs. current+retired."""
    entries = parse_sitemap(sitemap_bytes, path_pattern=r"/products/balls/(current)/")
    assert len(entries) == 4
    assert all(e["status"] == "current" for e in entries)


# --- diff_against_known: exercised against a small fake cursor/connection ---

class _FakeCursor:
    """Minimal stand-in for a psycopg2 cursor, backed by an in-memory dict
    of {url: lastmod} representing what's already in discovered_urls."""

    def __init__(self, known_urls):
        self._known = known_urls
        self._last_query_result = None

    def execute(self, query, params=None):
        query_lower = query.strip().lower()
        if query_lower.startswith("select"):
            (url,) = params
            existing_lastmod = self._known.get(url)
            self._last_query_result = (existing_lastmod,) if existing_lastmod is not None else None
        elif query_lower.startswith("insert"):
            _brand_id, url, _status, lastmod = params
            self._known[url] = lastmod
        elif query_lower.startswith("update") and "sitemap_lastmod" in query_lower:
            lastmod, url = params
            self._known[url] = lastmod
        # plain "update ... last_seen_at" (unchanged case) intentionally no-ops

    def fetchone(self):
        return self._last_query_result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self, known_urls):
        self._cursor = _FakeCursor(known_urls)
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


def test_diff_identifies_new_urls():
    conn = _FakeConnection(known_urls={})
    entries = [{"url": "https://example.com/a", "status": "current", "lastmod": "2026-01-01T00:00:00-05:00"}]

    diff = diff_against_known(conn, brand_id="fake-brand-id", entries=entries)

    assert diff["new"] == ["https://example.com/a"]
    assert diff["changed"] == []
    assert diff["unchanged"] == []
    assert conn.committed


def test_diff_identifies_changed_lastmod():
    conn = _FakeConnection(known_urls={"https://example.com/a": "2025-01-01T00:00:00-05:00"})
    entries = [{"url": "https://example.com/a", "status": "current", "lastmod": "2026-01-01T00:00:00-05:00"}]

    diff = diff_against_known(conn, brand_id="fake-brand-id", entries=entries)

    assert diff["changed"] == ["https://example.com/a"]
    assert diff["new"] == []


def test_diff_identifies_unchanged():
    conn = _FakeConnection(known_urls={"https://example.com/a": "2026-01-01T00:00:00-05:00"})
    entries = [{"url": "https://example.com/a", "status": "current", "lastmod": "2026-01-01T00:00:00-05:00"}]

    diff = diff_against_known(conn, brand_id="fake-brand-id", entries=entries)

    assert diff["unchanged"] == ["https://example.com/a"]
    assert diff["new"] == []
    assert diff["changed"] == []


def test_end_to_end_against_real_fixture(sitemap_bytes):
    """Parses the real fixture and runs it through the diff logic against
    an empty known-URLs store, as if this were the very first discovery run."""
    entries = parse_sitemap(sitemap_bytes)
    conn = _FakeConnection(known_urls={})

    diff = diff_against_known(conn, brand_id="fake-brand-id", entries=entries)

    assert len(diff["new"]) == 6
    assert len(diff["changed"]) == 0
    assert len(diff["unchanged"]) == 0


# --- Orchestration: build_scrape_messages / publish_messages ---

def test_build_scrape_messages_produces_one_message_per_url():
    messages = build_scrape_messages("brand-123", ["https://a.com/1", "https://a.com/2"])
    assert len(messages) == 2
    assert json.loads(messages[0]) == {"url": "https://a.com/1", "brand_id": "brand-123"}
    assert json.loads(messages[1]) == {"url": "https://a.com/2", "brand_id": "brand-123"}


def test_build_scrape_messages_empty_list():
    assert build_scrape_messages("brand-123", []) == []


class _FakeSqsClient:
    """Minimal stand-in for boto3's SQS client -- just records what would
    have been sent, no real AWS call."""

    def __init__(self):
        self.sent_batches = []

    def send_message_batch(self, QueueUrl, Entries):
        self.sent_batches.append((QueueUrl, Entries))
        return {"Successful": [{"Id": e["Id"]} for e in Entries], "Failed": []}


def test_publish_messages_sends_all_and_returns_count():
    sqs = _FakeSqsClient()
    sent = publish_messages(sqs, "https://sqs.example/queue", ["a", "b", "c"])
    assert sent == 3
    assert len(sqs.sent_batches) == 1
    assert len(sqs.sent_batches[0][1]) == 3


def test_publish_messages_batches_over_sqs_10_message_limit():
    """SendMessageBatch caps at 10 entries per call -- confirms
    publish_messages chunks rather than assuming small volume forever."""
    sqs = _FakeSqsClient()
    bodies = [f"msg-{i}" for i in range(23)]
    sent = publish_messages(sqs, "https://sqs.example/queue", bodies)
    assert sent == 23
    assert len(sqs.sent_batches) == 3
    assert [len(batch[1]) for batch in sqs.sent_batches] == [10, 10, 3]
