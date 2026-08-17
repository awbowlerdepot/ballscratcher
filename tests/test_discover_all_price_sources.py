"""
Tests for scripts/discover_all_price_sources.py.

Manual-runner pattern, run standalone via
`python3 tests/test_discover_all_price_sources.py`. Same shape as
test_refresh_video_stats.py's tests for the same reason -- this is a thin
POST to a bulk server-side endpoint (POST /admin/discover-all-price-
sources), not a list-then-iterate loop, since the actual selecting/
searching/inserting happens inside PriceCheckerFunction itself (see
service.queue_price_discovery_batch's docstring). Same injectable-session
pattern as every other script in this project for the same reason
(get_requests_session touches requests.Session/requests.adapters.
HTTPAdapter/urllib3.util.retry.Retry, so a bare fake module swap wouldn't
cover it).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import discover_all_price_sources as script  # noqa: E402


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


# --- trigger_discovery: single POST, no pagination ---

def test_trigger_discovery_posts_to_the_endpoint_with_limit():
    fake = _FakeSession(payload={"queued": True, "limit": 50})

    result = script.trigger_discovery("https://admin.example", "tok", limit=50, session=fake)

    assert len(fake.post_calls) == 1
    call = fake.post_calls[0]
    assert call["url"] == "https://admin.example/admin/discover-all-price-sources"
    assert call["params"] == {"limit": 50}
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert result == {"queued": True, "limit": 50}


def test_trigger_discovery_omits_limit_param_when_not_given():
    fake = _FakeSession()
    script.trigger_discovery("https://admin.example", "tok", session=fake)

    assert fake.post_calls[0]["params"] is None


def test_trigger_discovery_makes_exactly_one_call_no_pagination():
    fake = _FakeSession()
    script.trigger_discovery("https://admin.example", "tok", session=fake)
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


# --- run: repeat/interval orchestration around trigger_discovery() ---

def test_run_defaults_to_a_single_call_no_sleep(monkeypatch):
    calls = []
    sleeps = []
    monkeypatch.setattr(script, "trigger_discovery", lambda url, token, limit=None, scrape_only=False: {"queued": True, "limit": limit})

    results = script.run(
        "https://admin.example", "tok", limit=200,
        trigger_fn=lambda url, token, limit=None, scrape_only=False: calls.append(limit) or {"queued": True, "limit": limit},
        sleep_fn=lambda s: sleeps.append(s),
    )

    assert calls == [200]
    assert sleeps == []  # never sleeps after the only (and therefore last) call
    assert results == [{"queued": True, "limit": 200}]


def test_run_repeat_fires_multiple_times_sleeping_between_but_not_after():
    calls = []
    sleeps = []

    def fake_trigger(url, token, limit=None, scrape_only=False):
        calls.append(1)
        return {"queued": True, "limit": limit}

    results = script.run(
        "https://admin.example", "tok", repeat=3, interval_seconds=300,
        trigger_fn=fake_trigger, sleep_fn=lambda s: sleeps.append(s),
    )

    assert len(calls) == 3
    assert sleeps == [300, 300]  # between 1-2 and 2-3, never after the 3rd
    assert len(results) == 3


def test_run_stops_early_without_sleeping_when_not_queued():
    calls = []
    sleeps = []

    def fake_trigger(url, token, limit=None, scrape_only=False):
        calls.append(1)
        return {"queued": False, "reason": "PRICE_CHECKER_FUNCTION_NAME is not configured on this deployment"}

    results = script.run(
        "https://admin.example", "tok", repeat=5, interval_seconds=300,
        trigger_fn=fake_trigger, sleep_fn=lambda s: sleeps.append(s),
    )

    assert len(calls) == 1  # never retries a misconfiguration on a timer
    assert sleeps == []
    assert len(results) == 1
    assert results[0]["queued"] is False


def test_run_uses_real_time_sleep_by_default(monkeypatch):
    # Confirms the default sleep_fn really is time.sleep (not skipped
    # silently) -- monkeypatches the module's own time.sleep so this
    # doesn't actually pause the test suite.
    slept = []
    monkeypatch.setattr(script.time, "sleep", lambda s: slept.append(s))

    script.run(
        "https://admin.example", "tok", repeat=2, interval_seconds=5,
        trigger_fn=lambda url, token, limit=None, scrape_only=False: {"queued": True, "limit": limit},
    )

    assert slept == [5]


# --- scrape_only: Al, re-running catalog-wide discovery while testing a
# scrape-site config fix: "can we not run the bowlerdepot price sources
# in this one, they have inventory numbers too" -> "maybe just scrape
# sources" ---

def test_trigger_discovery_includes_scrape_only_param_when_true():
    fake = _FakeSession(payload={"queued": True, "limit": None, "scrape_only": True})
    script.trigger_discovery("https://admin.example", "tok", scrape_only=True, session=fake)

    assert fake.post_calls[0]["params"] == {"scrape_only": "true"}


def test_trigger_discovery_omits_scrape_only_param_when_false():
    fake = _FakeSession()
    script.trigger_discovery("https://admin.example", "tok", scrape_only=False, session=fake)

    # Default behavior unchanged -- no scrape_only key at all, not sent
    # as "false", same "omitted means default" convention as limit.
    assert fake.post_calls[0]["params"] is None


def test_trigger_discovery_combines_limit_and_scrape_only():
    fake = _FakeSession()
    script.trigger_discovery("https://admin.example", "tok", limit=50, scrape_only=True, session=fake)

    assert fake.post_calls[0]["params"] == {"limit": 50, "scrape_only": "true"}


def test_run_passes_scrape_only_through_to_trigger():
    captured = []

    def fake_trigger(url, token, limit=None, scrape_only=False):
        captured.append(scrape_only)
        return {"queued": True, "limit": limit}

    script.run(
        "https://admin.example", "tok", scrape_only=True,
        trigger_fn=fake_trigger, sleep_fn=lambda s: None,
    )

    assert captured == [True]


def test_main_reads_scrape_only_from_env(monkeypatch):
    captured = {}

    def fake_run(admin_api_url, token, limit=None, repeat=1, interval_seconds=300, scrape_only=False, trigger_fn=None, sleep_fn=None):
        captured["scrape_only"] = scrape_only
        return [{"queued": True}]

    monkeypatch.setattr(script, "run", fake_run)
    # Same manual os.environ save/restore convention as this project's
    # other manual-runner test files (e.g. test_admin_api_service.py) --
    # the local _MonkeyPatch shim below only implements setattr/undo, not
    # pytest's own setenv/delenv.
    saved = {k: os.environ.get(k) for k in ("ADMIN_API_URL", "ADMIN_API_TOKEN", "SCRAPE_ONLY")}
    os.environ["ADMIN_API_URL"] = "https://admin.example"
    os.environ["ADMIN_API_TOKEN"] = "tok"
    os.environ["SCRAPE_ONLY"] = "true"
    try:
        script.main()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    assert captured["scrape_only"] is True


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
