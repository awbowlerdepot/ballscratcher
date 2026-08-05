"""
Tests for scripts/backfill_last_video_discovery_at.py.

Manual-runner pattern, run standalone via
`python3 tests/test_backfill_last_video_discovery_at.py`.

Unlike backfill_core_ids.py/backfill_video_review_rollups.py, this script
has no list-then-iterate loop to test -- it's a single POST to a bulk
server-side endpoint (see service.backfill_last_video_discovery_at's
docstring for why the actual work happens in one SQL pass on the admin_api
side, not per-product from here). Same injectable-session pattern as
backfill_core_ids.py's tests for the same reason (get_requests_session
touches requests.Session/requests.adapters.HTTPAdapter/urllib3.util.retry.
Retry, so a bare fake module swap wouldn't cover it).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import backfill_last_video_discovery_at as script  # noqa: E402


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"products_with_video_history": 0, "products_updated": 0}
        self.post_calls = []

    def post(self, url, headers=None, timeout=None):
        self.post_calls.append({"url": url, "headers": headers, "timeout": timeout})
        return _FakeResponse(self.payload)


def test_backfill_posts_to_the_bulk_endpoint_and_returns_json():
    fake = _FakeSession(payload={"products_with_video_history": 57, "products_updated": 57})

    result = script.backfill("https://admin.example", "tok", session=fake)

    assert len(fake.post_calls) == 1
    call = fake.post_calls[0]
    assert call["url"] == "https://admin.example/admin/backfill-last-video-discovery-at"
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert result == {"products_with_video_history": 57, "products_updated": 57}


def test_backfill_makes_exactly_one_call_no_pagination():
    """Confirms this really is the thin one-shot design described in the
    module docstring -- not accidentally looping."""
    fake = _FakeSession()
    script.backfill("https://admin.example", "tok", session=fake)
    assert len(fake.post_calls) == 1


# --- get_requests_session: same retry-config sanity check as every other
# script in this project (see backfill_core_ids.py's test file for the
# fuller reasoning behind why 503 specifically matters here).

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


# --- run: thin orchestration wrapper around backfill() ---

def test_run_calls_backfill_and_returns_its_result(monkeypatch):
    monkeypatch.setattr(
        script, "backfill",
        lambda url, token: {"products_with_video_history": 12, "products_updated": 5},
    )

    result = script.run("https://admin.example", "tok")

    assert result == {"products_with_video_history": 12, "products_updated": 5}


def test_run_handles_zero_updates_without_error(monkeypatch):
    """A second/subsequent run after the backfill has already caught
    everything up returns products_updated=0 -- not an error, just
    nothing left to do (see service.backfill_last_video_discovery_at's
    idempotency docs)."""
    monkeypatch.setattr(
        script, "backfill",
        lambda url, token: {"products_with_video_history": 12, "products_updated": 0},
    )

    result = script.run("https://admin.example", "tok")

    assert result == {"products_with_video_history": 12, "products_updated": 0}


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
