"""
Tests for scripts/reestimate_plotter_positions.py.

Manual-runner pattern, run standalone via
`python3 tests/test_reestimate_plotter_positions.py`.

Same shape as test_backfill_last_video_discovery_at.py -- this script is
also a single POST to a bulk server-side endpoint (see service.
reestimate_plotter_positions' docstring), not a list-then-iterate loop.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import reestimate_plotter_positions as script  # noqa: E402


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"products_estimated": 0, "products_updated": 0}
        self.post_calls = []

    def post(self, url, headers=None, timeout=None):
        self.post_calls.append({"url": url, "headers": headers, "timeout": timeout})
        return _FakeResponse(self.payload)


def test_reestimate_posts_to_the_bulk_endpoint_and_returns_json():
    fake = _FakeSession(payload={"products_estimated": 143, "products_updated": 143})

    result = script.reestimate("https://admin.example", "tok", session=fake)

    assert len(fake.post_calls) == 1
    call = fake.post_calls[0]
    assert call["url"] == "https://admin.example/admin/reestimate-plotter-positions"
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert result == {"products_estimated": 143, "products_updated": 143}


def test_reestimate_makes_exactly_one_call_no_pagination():
    fake = _FakeSession()
    script.reestimate("https://admin.example", "tok", session=fake)
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


# --- run: thin orchestration wrapper around reestimate() ---

def test_run_calls_reestimate_and_returns_its_result(monkeypatch):
    monkeypatch.setattr(
        script, "reestimate",
        lambda url, token: {"products_estimated": 143, "products_updated": 118},
    )

    result = script.run("https://admin.example", "tok")

    assert result == {"products_estimated": 143, "products_updated": 118}


def test_run_handles_zero_updates_without_error(monkeypatch):
    """A second/subsequent run, after the first already caught everything
    up (or a deploy that changed nothing about the formula), returns
    products_updated=0 -- not an error, just nothing left to fix."""
    monkeypatch.setattr(
        script, "reestimate",
        lambda url, token: {"products_estimated": 143, "products_updated": 0},
    )

    result = script.run("https://admin.example", "tok")

    assert result == {"products_estimated": 143, "products_updated": 0}


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
