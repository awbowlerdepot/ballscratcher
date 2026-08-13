"""
Tests for scripts/refresh_video_stats.py.

Manual-runner pattern, run standalone via
`python3 tests/test_refresh_video_stats.py`.

Same shape as test_backfill_last_video_discovery_at.py's tests for the
same reason -- this is a thin one-shot POST to a bulk server-side
endpoint (POST /admin/refresh-video-stats), not a list-then-iterate loop,
since the actual selecting/fetching/updating happens inside
VideoDiscoveryFunction itself (see service.queue_video_stats_refresh's
docstring). Same injectable-session pattern as every other script in this
project for the same reason (get_requests_session touches requests.
Session/requests.adapters.HTTPAdapter/urllib3.util.retry.Retry, so a bare
fake module swap wouldn't cover it).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import refresh_video_stats as script  # noqa: E402


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"queued": True, "limit": None}
        self.post_calls = []

    def post(self, url, params=None, headers=None, timeout=None):
        self.post_calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return _FakeResponse(self.payload)


def test_trigger_refresh_posts_to_the_endpoint_with_limit():
    fake = _FakeSession(payload={"queued": True, "limit": 50})

    result = script.trigger_refresh("https://admin.example", "tok", limit=50, session=fake)

    assert len(fake.post_calls) == 1
    call = fake.post_calls[0]
    assert call["url"] == "https://admin.example/admin/refresh-video-stats"
    assert call["params"] == {"limit": 50}
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert result == {"queued": True, "limit": 50}


def test_trigger_refresh_omits_limit_param_when_not_given():
    fake = _FakeSession()
    script.trigger_refresh("https://admin.example", "tok", session=fake)

    assert fake.post_calls[0]["params"] is None


def test_trigger_refresh_makes_exactly_one_call_no_pagination():
    fake = _FakeSession()
    script.trigger_refresh("https://admin.example", "tok", session=fake)
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


# --- run: thin orchestration wrapper around trigger_refresh() ---

def test_run_calls_trigger_and_returns_its_result(monkeypatch):
    monkeypatch.setattr(
        script, "trigger_refresh",
        lambda url, token, limit=None: {"queued": True, "limit": limit},
    )

    result = script.run("https://admin.example", "tok", limit=200)

    assert result == {"queued": True, "limit": 200}


def test_run_passes_none_limit_through_by_default(monkeypatch):
    captured = {}

    def fake_trigger(url, token, limit=None):
        captured["limit"] = limit
        return {"queued": True, "limit": None}

    monkeypatch.setattr(script, "trigger_refresh", fake_trigger)
    script.run("https://admin.example", "tok")

    assert captured["limit"] is None


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
