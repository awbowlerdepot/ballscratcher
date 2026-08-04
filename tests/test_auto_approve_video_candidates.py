"""
Tests for scripts/auto_approve_video_candidates.py.

Manual-runner pattern, run standalone via
`python3 tests/test_auto_approve_video_candidates.py`. Same
sys.modules-faking approach for `requests` as
test_home_transcript_fetcher.py (this script does `import requests`
inside its functions, not at module top level).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import auto_approve_video_candidates as script  # noqa: E402


# --- split_by_confidence ---

def test_split_by_confidence_separates_high_and_low():
    candidates = [
        {"id": "a", "match_confidence": "high"},
        {"id": "b", "match_confidence": "low"},
        {"id": "c", "match_confidence": "high"},
    ]
    high, low = script.split_by_confidence(candidates)
    assert [c["id"] for c in high] == ["a", "c"]
    assert [c["id"] for c in low] == ["b"]


def test_split_by_confidence_treats_unexpected_value_as_low():
    """A missing/unrecognized match_confidence shouldn't get auto-approved
    -- default to the human-review pile, not the other way around."""
    candidates = [{"id": "a", "match_confidence": "medium"}, {"id": "b"}]
    high, low = script.split_by_confidence(candidates)
    assert high == []
    assert [c["id"] for c in low] == ["a", "b"]


# --- list_pending_candidates: fake requests, paginates ---

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeRequestsModule:
    def __init__(self, pages):
        self.pages = pages
        self.get_calls = []
        self.post_calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.get_calls.append({"url": url, "params": params, "headers": headers})
        page = self.pages.pop(0)
        return _FakeResponse(page)

    def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse({})


def test_list_pending_candidates_paginates():
    real_requests = sys.modules.get("requests")
    fake = _FakeRequestsModule(pages=[
        {"items": [{"id": f"vid-{i}", "match_confidence": "high"} for i in range(200)]},
        {"items": [{"id": "vid-200", "match_confidence": "low"}]},
    ])
    sys.modules["requests"] = fake
    try:
        result = script.list_pending_candidates("https://admin.example", "tok", page_limit=200)
        assert len(result) == 201
        assert len(fake.get_calls) == 2
        assert fake.get_calls[0]["params"] == {"status": "pending", "limit": 200, "offset": 0}
        assert fake.get_calls[1]["params"] == {"status": "pending", "limit": 200, "offset": 200}
        assert fake.get_calls[0]["headers"]["Authorization"] == "Bearer tok"
    finally:
        if real_requests is not None:
            sys.modules["requests"] = real_requests
        else:
            del sys.modules["requests"]


# --- approve_candidate: fake requests, posts to the right URL ---

def test_approve_candidate_posts_resolved_by():
    real_requests = sys.modules.get("requests")
    fake = _FakeRequestsModule(pages=[])
    sys.modules["requests"] = fake
    try:
        script.approve_candidate("https://admin.example", "tok", "vid-1", "auto-approve-script")
        assert len(fake.post_calls) == 1
        call = fake.post_calls[0]
        assert call["url"] == "https://admin.example/video-candidates/vid-1/approve"
        assert call["json"] == {"resolved_by": "auto-approve-script"}
        assert call["headers"]["Authorization"] == "Bearer tok"
    finally:
        if real_requests is not None:
            sys.modules["requests"] = real_requests
        else:
            del sys.modules["requests"]


# --- run: the orchestration -- approves high, leaves low, tolerates errors ---

def test_run_approves_high_confidence_and_leaves_low_for_review(monkeypatch):
    candidates = [
        {"id": "vid-1", "match_confidence": "high", "title": "Storm Absolute Review"},
        {"id": "vid-2", "match_confidence": "low", "title": "bowling tips"},
        {"id": "vid-3", "match_confidence": "high", "title": "Roto Grip Idol Review"},
    ]
    monkeypatch.setattr(script, "list_pending_candidates", lambda url, token: candidates)

    approved = []
    monkeypatch.setattr(script, "approve_candidate", lambda url, token, vid, resolved_by: approved.append((vid, resolved_by)))

    summary = script.run("https://admin.example", "tok", resolved_by="auto-approve-script")

    assert summary == {"total_pending": 3, "approved": 2, "errors": 0, "left_for_review": 1}
    assert approved == [("vid-1", "auto-approve-script"), ("vid-3", "auto-approve-script")]


def test_run_tolerates_per_candidate_approval_errors(monkeypatch):
    """One candidate failing to approve (e.g. a real race -- someone else
    approved/rejected it a second earlier) shouldn't stop the rest of the
    batch, same principle as home_transcript_fetcher.run()."""
    candidates = [
        {"id": "vid-1", "match_confidence": "high"},
        {"id": "vid-2", "match_confidence": "high"},
    ]
    monkeypatch.setattr(script, "list_pending_candidates", lambda url, token: candidates)

    def flaky_approve(url, token, video_id, resolved_by):
        if video_id == "vid-1":
            raise RuntimeError("simulated 422 -- already resolved")

    monkeypatch.setattr(script, "approve_candidate", flaky_approve)

    summary = script.run("https://admin.example", "tok", resolved_by="auto-approve-script")

    assert summary == {"total_pending": 2, "approved": 1, "errors": 1, "left_for_review": 0}


def test_run_with_no_pending_candidates():
    def empty_list(url, token):
        return []

    summary = script.run("https://admin.example", "tok", resolved_by="auto-approve-script", list_fn=empty_list)
    assert summary == {"total_pending": 0, "approved": 0, "errors": 0, "left_for_review": 0}


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
