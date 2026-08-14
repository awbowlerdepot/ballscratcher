"""
Tests for scripts/dedupe_product_price_sources.py.

Manual-runner pattern, run standalone via
`python3 tests/test_dedupe_product_price_sources.py`.

Same thin-one-shot-POST shape as test_backfill_last_video_discovery_at.py
-- no list-then-iterate loop to test, since the actual merge work happens
in one SQL pass on the admin_api side (see service.dedupe_product_price_
sources' docstring). Same injectable-session pattern as that test file's
own get_requests_session tests.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import dedupe_product_price_sources as script  # noqa: E402


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"groups_merged": 0, "rows_deleted": 0}
        self.post_calls = []

    def post(self, url, headers=None, timeout=None):
        self.post_calls.append({"url": url, "headers": headers, "timeout": timeout})
        return _FakeResponse(self.payload)


def test_dedupe_posts_to_the_bulk_endpoint_and_returns_json():
    fake = _FakeSession(payload={"groups_merged": 4, "rows_deleted": 4})

    result = script.dedupe("https://admin.example", "tok", session=fake)

    assert len(fake.post_calls) == 1
    call = fake.post_calls[0]
    assert call["url"] == "https://admin.example/admin/dedupe-price-sources"
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert result == {"groups_merged": 4, "rows_deleted": 4}


def test_dedupe_makes_exactly_one_call_no_pagination():
    """Confirms this really is the thin one-shot design described in the
    module docstring -- not accidentally looping."""
    fake = _FakeSession()
    script.dedupe("https://admin.example", "tok", session=fake)
    assert len(fake.post_calls) == 1


# --- get_requests_session: same retry-config sanity check as every other
# script in this project.

def test_get_requests_session_retries_on_throttle_and_5xx_status_codes():
    session = script.get_requests_session()
    adapter = session.get_adapter("https://admin.example")
    retry = adapter.max_retries

    assert retry.total == script.RETRY_TOTAL
    assert set(retry.status_forcelist) == set(script.RETRY_STATUS_FORCELIST)
    assert 503 in retry.status_forcelist
    assert "POST" in retry.allowed_methods
    assert retry.backoff_factor == script.RETRY_BACKOFF_FACTOR


def test_get_requests_session_returns_a_fresh_session_each_call():
    assert script.get_requests_session() is not script.get_requests_session()


# --- run: thin orchestration wrapper around dedupe() ---

def test_run_calls_dedupe_and_returns_its_result(monkeypatch):
    monkeypatch.setattr(
        script, "dedupe",
        lambda url, token: {"groups_merged": 3, "rows_deleted": 3},
    )

    result = script.run("https://admin.example", "tok")

    assert result == {"groups_merged": 3, "rows_deleted": 3}


def test_run_handles_zero_duplicates_without_error(monkeypatch):
    """A second/subsequent run after the dedupe has already caught
    everything up returns groups_merged=0 -- not an error, just nothing
    left to do (see service.dedupe_product_price_sources' idempotency
    docs)."""
    monkeypatch.setattr(
        script, "dedupe",
        lambda url, token: {"groups_merged": 0, "rows_deleted": 0},
    )

    result = script.run("https://admin.example", "tok")

    assert result == {"groups_merged": 0, "rows_deleted": 0}


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
